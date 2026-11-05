"""The case table: every office tool × normal / edge / error.

A case is a tool name, the arguments it is called with, the fixtures that have
to be staged into its workspace, and what kind of answer is expected. The
runner executes each one through `ToolRegistry.execute` and records what came
back; nothing here asserts anything, because the point is to capture the
Python's behaviour rather than to agree with a prediction about it.

`note` is where the reason a case exists goes, and a case that looks
redundant usually is not: `read_excel_sheet/hidden-col-range` reads a sheet
that exists only to carry one `<col min=3 max=5 hidden=1>` element, because
that is the single place the port's output is meant to differ from the
Python's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# Fixtures every case can name; the runner copies only what a case lists.
ALL_FIXTURES = (
    "databook.xlsx", "broken.xlsx", "doc.docx", "template.pptx",
    "note.pdf", "rot.pdf", "img.png", "img.jpg", "img.gif", "nb.ipynb",
)

_CJK_PARAGRAPH = (
    "現金及現金等價物由二零二四年的一千四百一十萬元增加至二零二五年的"
    "二千五百六十四萬元，主要由於營運資金改善。"
)
_LATIN_PARAGRAPH = (
    "The cash balance increased from 1,410 to 25,641 over the period, "
    "driven by improved working capital."
)
# Sized to overflow the customInsets box by roughly one line, so the autofit
# scale lands strictly inside the [0.70, 1.0] clamp.
_MILD_OVERFLOW_PARAGRAPH = (
    "The cash balance increased from 1,410 to 25,641 over the period, driven "
    "by improved working capital and a shorter collection cycle. Trade "
    "payables lengthened by eleven days, which funded most of the movement, "
    "and the revolving facility was undrawn at both balance sheet dates."
)


@dataclass(frozen=True)
class ProjectionSpec:
    extractor: str
    of: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Case:
    id: str
    tool: str
    args: dict[str, Any]
    kind: Literal["json", "text"] = "json"
    expect: Literal["ok", "error"] = "ok"
    files: tuple[str, ...] = ()
    pre_steps: tuple[tuple[str, dict], ...] = ()
    projection: ProjectionSpec | None = None
    font_variant: str | None = None
    note: str = ""


DB = ("databook.xlsx",)
DOC = ("doc.docx",)
PPT = ("template.pptx",)
PDF = ("note.pdf",)


def excel_cases() -> list[Case]:
    return [
        Case(
            "read_excel_sheet/data", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "Data"}, files=DB,
            note=(
                "The str() parity row. True, 100, 100.0, 1e-07, 1e+16, "
                "0.30000000000000004, a datetime, a datetime with a time, a "
                "timedelta and an #N/A all reach the model as Python's str(), "
                "which JavaScript's String() does not reproduce for the two "
                "exponent forms."
            ),
        ),
        Case(
            "read_excel_sheet/hidden-col-range", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "HiddenRange"}, files=DB,
            note=(
                "THE DELIBERATE DEVIATION, and the only case whose output the "
                "port is expected to differ on. The sheet's sole feature is "
                "<col min=3 max=5 hidden=1>, which openpyxl collapses into one "
                "ColumnDimension keyed 'C'; _get_visibility_state "
                "(office.py:537-553) therefore returns {3} and D/E stay "
                "visible, so the Python reports five headers and '1 hidden "
                "column'. The port hides 3..5 and should report three headers "
                "and '3 hidden columns'. It lives on its own sheet precisely "
                "so that no other case inherits the quirk."
            ),
        ),
        Case(
            "read_excel_sheet/cash-offset-origin", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "Cash"}, files=DB,
            note=(
                "The used range starts at C3, but iter_rows() walks from row 1 "
                "of the bounding box, so rows 1-2 come back entirely blank and "
                "the HEADER is an all-empty row rather than 'Item'/'Amount'. "
                "Leading blank columns are \"\" too. A port that starts at the "
                "declared dimension origin produces a completely different, and "
                "more sensible, answer — which is why this is pinned."
            ),
        ),
        Case(
            "read_excel_sheet/max-rows-1", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "Data", "max_rows": 1}, files=DB,
            note="break tests len(rows) >= max_rows + 1 BEFORE appending.",
        ),
        Case(
            "read_excel_sheet/empty-sheet", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "Empty"}, files=DB,
            note='No rows at all returns {"headers": [], "rows": []} with no note.',
        ),
        Case(
            "read_excel_sheet/unknown-sheet", "read_excel_sheet",
            {"path": "databook.xlsx", "sheet": "NoSuchSheet"}, files=DB,
            expect="error", note="openpyxl raises KeyError, which propagates uncaught.",
        ),
        Case(
            "list_excel_sheets/hidden-and-veryhidden", "list_excel_sheets",
            {"path": "databook.xlsx"}, files=DB,
            note=(
                "hidden and veryHidden both count toward hidden_sheets_count, "
                "and that key appears only when the count is > 0."
            ),
        ),
        Case(
            "list_excel_sheets/not-a-zip", "list_excel_sheets",
            {"path": "broken.xlsx"}, files=("broken.xlsx",), expect="error",
            note="zipfile.BadZipFile from openpyxl, surfaced as-is.",
        ),
        Case(
            "inspect_excel_sheets/default", "inspect_excel_sheets",
            {"path": "databook.xlsx"}, files=DB,
            note=(
                "estimated_data_rows = max(0, max_row - 1 - len(hidden_rows)); "
                "the key is hidden_sheets, not hidden_sheets_count."
            ),
        ),
        Case(
            "inspect_excel_sheets/sample-rows-1", "inspect_excel_sheets",
            {"path": "databook.xlsx", "sample_rows": 1}, files=DB,
            note="append-then-break at sample_rows + 1, the opposite order to read_excel_sheet.",
        ),
        Case(
            "inspect_excel_sheets/missing-file", "inspect_excel_sheets",
            {"path": "nope.xlsx"}, files=DB, expect="error",
        ),
        Case(
            "excel_to_json/data", "excel_to_json",
            {"path": "databook.xlsx", "sheet": "Data"}, files=DB,
            note=(
                "The result is a JSON array, and column I has a deliberately "
                "blank header so the col_{i} fallback (office.py:637) is "
                "reached. Note the index counts VISIBLE columns, after the "
                "hidden ones are dropped."
            ),
        ),
        Case(
            "excel_to_json/empty-sheet", "excel_to_json",
            {"path": "databook.xlsx", "sheet": "Empty"}, files=DB,
            note=(
                'Returns the bare literal "[]" without going through '
                "json.dumps — same parse, different bytes."
            ),
        ),
        Case(
            "excel_to_json/max-rows-0", "excel_to_json",
            {"path": "databook.xlsx", "sheet": "Data", "max_rows": 0}, files=DB,
            note="max_rows 0 still admits the header row (>= max_rows + 1).",
        ),
        Case(
            "read_excel_preview/data", "read_excel_preview",
            {"path": "databook.xlsx", "sheet": "Data"}, files=DB,
            note=(
                "_is_numeric is Python float(s.replace(',','')), which "
                "accepts 1_000, inf, +5, 1. and -.5 and rejects 0x10 — none of "
                "which this fixture contains, so that grammar is pinned "
                "nowhere and a port should treat it as unverified. What this "
                "case does pin is the 70% rule over the first 20 truthy rows "
                "and preview = data[:10]. Column I is all numeric, so the "
                "\"numeric\" branch of the inference has coverage; every other "
                "column sits at 2/3 and infers \"text\"."
            ),
        ),
        Case(
            "read_excel_preview/max-rows-1", "read_excel_preview",
            {"path": "databook.xlsx", "sheet": "Data", "max_rows": 1}, files=DB,
        ),
        Case(
            "read_excel_preview/empty-sheet", "read_excel_preview",
            {"path": "databook.xlsx", "sheet": "Empty"}, files=DB,
        ),
        Case(
            "locate_databook_stage_columns/found", "locate_databook_stage_columns",
            {"path": "databook.xlsx", "sheet": "Stages"}, files=DB,
            note=(
                "Scans raw values INCLUDING hidden rows — the DATE row is hidden "
                "on purpose, so a port that iterated visible rows would not "
                "find date_row_idx at all. Label order puts 示意性调整数/數 "
                "before 示意性调整; the serial window is 32874..54970."
            ),
        ),
        Case(
            "locate_databook_stage_columns/scan-1-row", "locate_databook_stage_columns",
            {"path": "databook.xlsx", "sheet": "Stages", "max_scan_rows": 1}, files=DB,
            note="scan_end = min(min_row + max_scan_rows, max_row).",
        ),
        Case(
            "locate_databook_stage_columns/not-found", "locate_databook_stage_columns",
            {"path": "databook.xlsx", "sheet": "Cash"}, files=DB,
            note="No basis labels at all: found is false rather than an error.",
        ),
        Case(
            "write_excel_cell/string", "write_excel_cell",
            {"path": "databook.xlsx", "sheet": "Data", "cell": "A6", "value": "written"},
            kind="text", files=DB,
            projection=ProjectionSpec("xlsx_cell", "databook.xlsx",
                                      {"sheet": "Data", "ref": "A6"}),
            note=(
                "The return string carries path.name, not the full path. The "
                "projection is what matters: openpyxl's round-trip drops the "
                "cached <v> from D2, which the port's in-place edit will not."
            ),
        ),
        Case(
            "write_excel_cell/formula", "write_excel_cell",
            {"path": "databook.xlsx", "sheet": "Data", "cell": "B6", "value": "=SUM(B2:B3)"},
            kind="text", files=DB,
            projection=ProjectionSpec("xlsx_cell", "databook.xlsx",
                                      {"sheet": "Data", "ref": "B6"}),
            note="A leading = makes it a formula; openpyxl writes no cached value.",
        ),
        Case(
            "write_excel_cell/bad-ref", "write_excel_cell",
            {"path": "databook.xlsx", "sheet": "Data", "cell": "1A", "value": "x"},
            kind="text", files=DB, expect="error",
        ),
    ]


def word_cases() -> list[Case]:
    return [
        Case(
            "read_word_text/full", "read_word_text", {"path": "doc.docx"},
            kind="text", files=DOC,
            note=(
                "Body-level w:p only — the two paragraphs inside the w:tbl must "
                "NOT appear. The fixture exercises w:tab and w:br; w:cr takes "
                "the same branch but no fixture emits one."
            ),
        ),
        Case(
            "read_word_text/truncated", "read_word_text",
            {"path": "doc.docx", "max_chars": 20}, kind="text", files=DOC,
            note="Truncation counts code points, and appends a marker line.",
        ),
        Case(
            "read_word_text/missing", "read_word_text", {"path": "nope.docx"},
            kind="text", files=DOC, expect="error",
        ),
        Case(
            "read_word_paragraphs/all", "read_word_paragraphs", {"path": "doc.docx"},
            files=DOC,
            note=(
                "style is the UI name, so the Heading1 styleId comes back as "
                "'Heading 1'. A paragraph with no pStyle reports the document's "
                "default paragraph style."
            ),
        ),
        Case(
            "read_word_paragraphs/limit-2", "read_word_paragraphs",
            {"path": "doc.docx", "max_paragraphs": 2}, files=DOC,
        ),
        Case(
            "read_word_paragraphs/missing", "read_word_paragraphs", {"path": "nope.docx"},
            files=DOC, expect="error",
        ),
        Case(
            "write_word/styles", "write_word",
            {
                "path": "out/report.docx",
                "paragraphs": [
                    "Plain paragraph",
                    {"text": "The title", "style": "Title"},
                    {"text": "A heading", "style": "Heading 1"},
                    {"text": "A bullet", "style": "List Bullet"},
                    {"text": "現金及現金等價物", "style": "Normal"},
                ],
            },
            kind="text",
            projection=ProjectionSpec("docx_paragraphs", "out/report.docx"),
            note=(
                "Parent directories are created. A paragraph written with the "
                "default style must carry NO pStyle element at all; the "
                "projection's styles_present proves 'Heading 1' resolved to the "
                "Heading1 styleId rather than being written verbatim."
            ),
        ),
        Case(
            "write_word/unknown-style", "write_word",
            {"path": "out/bad.docx", "paragraphs": [{"text": "x", "style": "No Such Style"}]},
            kind="text", expect="error",
            note="python-docx raises KeyError(\"no style with name 'X'\").",
        ),
        Case(
            "append_word/normal", "append_word",
            {"path": "doc.docx", "text": "Appended line"}, kind="text", files=DOC,
            projection=ProjectionSpec("docx_paragraphs", "doc.docx"),
            note="Inserted before w:sectPr, so it is the last body paragraph.",
        ),
        Case(
            "append_word/heading", "append_word",
            {"path": "doc.docx", "text": "Appended heading", "style": "Heading 2"},
            kind="text", files=DOC,
            projection=ProjectionSpec("docx_paragraphs", "doc.docx"),
        ),
        Case(
            "append_word/missing", "append_word", {"path": "nope.docx", "text": "x"},
            kind="text", files=DOC, expect="error",
        ),
    ]


def pptx_read_cases() -> list[Case]:
    return [
        Case(
            "read_pptx/template", "read_pptx", {"path": "template.pptx"}, files=PPT,
            note=(
                "Only p:sp shapes carry text — the picture, chart, table and "
                "group must contribute none. a:br renders as \\v. Empty shapes "
                "are skipped after stripping."
            ),
        ),
        Case(
            "read_pptx/max-slides-1", "read_pptx",
            {"path": "template.pptx", "max_slides": 1}, files=PPT,
        ),
        Case(
            "read_pptx/missing", "read_pptx", {"path": "nope.pptx"}, files=PPT,
            expect="error",
        ),
        Case(
            "inspect_pptx_shapes/template", "inspect_pptx_shapes",
            {"path": "template.pptx"}, files=PPT,
            note=(
                "Every branch of the shape_type mapping appears on slide 0: "
                "TEXT_BOX (17), TABLE (19), AUTO_SHAPE (1), LINE (9), "
                "PICTURE (13), GROUP (6), FREEFORM (5), CHART (3); slide 1 "
                "supplies PLACEHOLDER (14). Slide 1's placeholders have no "
                "a:xfrm, so their dimensions must be inherited from the layout "
                "and then the master. round(emu/914400, 2) is half-even."
            ),
        ),
        Case(
            "inspect_pptx_shapes/missing", "inspect_pptx_shapes", {"path": "nope.pptx"},
            files=PPT, expect="error",
        ),
    ]


def metrics_cases(variant: str) -> list[Case]:
    """Capacity and fill cases, run once per font variant."""
    suffix = f"@{variant}"
    return [
        Case(
            f"estimate_pptx_text_capacity/latin-fits{suffix}",
            "estimate_pptx_text_capacity",
            {
                "path": "template.pptx", "slide_index": 0,
                "shape_name": "textMainBullets_L", "paragraphs": ["Cash increased."],
            },
            files=PPT, font_variant=variant,
            note="line_h = ascent + descent as integers; para_height = lines*line_h + 3.",
        ),
        Case(
            f"estimate_pptx_text_capacity/latin-overflows{suffix}",
            "estimate_pptx_text_capacity",
            {
                "path": "template.pptx", "slide_index": 3,
                "shape_name": "tinyBox", "paragraphs": [_LATIN_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            note="fill_ratio is rounded to 2 dp with Python's half-even round().",
        ),
        Case(
            f"estimate_pptx_text_capacity/cjk-auto-detect{suffix}",
            "estimate_pptx_text_capacity",
            {
                "path": "template.pptx", "slide_index": 0,
                "shape_name": "textMainBullets_L", "paragraphs": [_CJK_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            note=(
                "is_chinese is omitted, so it is inferred from a >30% CJK "
                "ratio over non-space characters. Kinsoku (text_metrics.py:115) "
                "does NOT fire on this text — no wrapped line begins with a "
                "kinsoku character — so the pull-back and the empty-line "
                "deletion are both unverified; check per_paragraph.lines "
                "before assuming otherwise."
            ),
        ),
        Case(
            f"estimate_pptx_text_capacity/custom-insets{suffix}",
            "estimate_pptx_text_capacity",
            {
                "path": "template.pptx", "slide_index": 0,
                "shape_name": "customInsets", "paragraphs": [_LATIN_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            note=(
                "The shape's own bodyPr sets lIns/rIns 0 and tIns/bIns 180000. "
                "A port that always subtracts the OOXML defaults gets a "
                "different usable box here and nowhere else."
            ),
        ),
        Case(
            f"estimate_pptx_text_capacity/table-shape{suffix}",
            "estimate_pptx_text_capacity",
            {
                "path": "template.pptx", "slide_index": 0,
                "shape_name": "Table Placeholder", "paragraphs": ["x"],
            },
            files=PPT, expect="error", font_variant=variant,
        ),
        Case(
            f"fill_pptx_shape_text/fits{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/fits.pptx",
                "slide_index": 0, "shape_name": "textMainBullets_L",
                "paragraphs": ["Cash increased.", "Receivables fell."],
            },
            files=PPT, font_variant=variant,
            projection=ProjectionSpec("pptx_text_shape", "out/fits.pptx",
                                      {"slide_index": 0, "shape_name": "textMainBullets_L"}),
            note=(
                "Runs carry sz=900 and a:latin only — no a:ea. Spacing is "
                "lnSpc 100%, spcBef 0, spcAft 3pt, in schema order. Fitting "
                "text gets noAutofit."
            ),
        ),
        Case(
            f"fill_pptx_shape_text/overflow-shrink{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/shrink.pptx",
                "slide_index": 3, "shape_name": "tinyBox",
                "paragraphs": [_LATIN_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            projection=ProjectionSpec("pptx_text_shape", "out/shrink.pptx",
                                      {"slide_index": 3, "shape_name": "tinyBox"}),
            note=(
                "normAutofit fontScale = round(scale*1e5), lnSpcReduction = "
                "round(max(0, (1-scale)*0.5)*1e5), scale clamped to [0.70, 1.0]."
            ),
        ),
        Case(
            f"fill_pptx_shape_text/stale-autofit{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/stale.pptx",
                "slide_index": 0, "shape_name": "staleAutofit",
                "paragraphs": ["short"],
            },
            files=PPT, font_variant=variant,
            projection=ProjectionSpec("pptx_text_shape", "out/stale.pptx",
                                      {"slide_index": 0, "shape_name": "staleAutofit"}),
            note=(
                "The shape ships BOTH a:spAutoFit (from add_textbox) and a stale "
                "a:normAutofit fontScale=62500. _clear_autofit has to remove "
                "both, which is what autofit_child_count in the projection "
                "checks."
            ),
        ),
        Case(
            f"fill_pptx_shape_text/cjk{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/cjk.pptx",
                "slide_index": 0, "shape_name": "textMainBullets_L",
                "paragraphs": [_CJK_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            projection=ProjectionSpec("pptx_text_shape", "out/cjk.pptx",
                                      {"slide_index": 0, "shape_name": "textMainBullets_L"}),
            note="Auto-detected CJK switches a:latin to Microsoft YaHei.",
        ),
        Case(
            f"fill_pptx_shape_text/unknown-shape{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/x.pptx",
                "slide_index": 0, "shape_name": "doesNotExist", "paragraphs": ["hi"],
            },
            files=PPT, expect="error", font_variant=variant,
            note=(
                "The message embeds a Python repr(list) of the available shape "
                "names, not JSON. Assert on the structured parts, not the text."
            ),
        ),
        Case(
            f"fill_pptx_shape_text/slide-out-of-range{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/x.pptx",
                "slide_index": 99, "shape_name": "textMainBullets_L", "paragraphs": ["hi"],
            },
            files=PPT, expect="error", font_variant=variant,
        ),
        Case(
            f"fill_pptx_shape_text/table-shape{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/x.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder", "paragraphs": ["hi"],
            },
            files=PPT, expect="error", font_variant=variant,
            note="The message must contain 'use fill_pptx_table instead'.",
        ),
        Case(
            f"fill_pptx_shape_text/overflow-mild{suffix}", "fill_pptx_shape_text",
            {
                "path": "template.pptx", "output_path": "out/mild.pptx",
                "slide_index": 0, "shape_name": "customInsets",
                "paragraphs": [_MILD_OVERFLOW_PARAGRAPH],
            },
            files=PPT, font_variant=variant,
            projection=ProjectionSpec("pptx_text_shape", "out/mild.pptx",
                                      {"slide_index": 0, "shape_name": "customInsets"}),
            note=(
                "A scale strictly inside (0.70, 1.0). The tinyBox case "
                "overflows so badly that max(0.70, ...) wins, so fontScale "
                "lands exactly on the clamp and the formula "
                "round(scale * 1e5) is never observed on a computed value -- "
                "a port with the wrong formula but the right clamp passed it."
            ),
        ),
        Case(
            f"fill_pptx_shape_text/progressive{suffix}", "fill_pptx_shape_text",
            {
                "path": "out/prog.pptx", "output_path": "out/prog.pptx",
                "slide_index": 0, "shape_name": "customInsets",
                "paragraphs": ["Filled second, into the file the first pass wrote."],
            },
            files=PPT, font_variant=variant,
            pre_steps=(("fill_pptx_shape_text", {
                "path": "template.pptx", "output_path": "out/prog.pptx",
                "slide_index": 0, "shape_name": "textMainBullets_L",
                "paragraphs": ["Filled first."],
            }),),
            projection=ProjectionSpec("pptx_package", "out/prog.pptx"),
            note=(
                "The contract the tool's own description promises: pass the "
                "same path as input and output to keep filling a deck shape "
                "by shape. Both fills must survive. Nothing exercised it, and "
                "nothing used pre_steps either -- the mechanism existed in "
                "the runner with no case behind it."
            ),
        ),
    ]


def pptx_write_cases() -> list[Case]:
    return [
        Case(
            "fill_pptx_table/rebuild-3x3", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/table.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder",
                "rows": [["Account", "FY24", "FY25"], ["Cash", "10.0", "12.0"],
                         ["AR", "5.0", "6.0"]],
            },
            files=PPT,
            projection=ProjectionSpec("pptx_table_shape", "out/table.pptx",
                                      {"slide_index": 0, "shape_name": "Table Placeholder"}),
            note=(
                "Delete and rebuild, never fill in place. The new graphicFrame "
                "is APPENDED, so spTree_index jumps to the end of the z-order and "
                "id is max(//@id)+1. 3657600/3 divides evenly, so the "
                "remainder rule is asserted by the remainder-columns case, "
                "not this one."
            ),
        ),
        Case(
            "fill_pptx_table/ragged-rows", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/ragged.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder",
                "rows": [["A", "B", "C"], ["only one"], ["x", "y"]],
            },
            files=PPT,
            projection=ProjectionSpec("pptx_table_shape", "out/ragged.pptx",
                                      {"slide_index": 0, "shape_name": "Table Placeholder"}),
            note="Column count comes from the widest row; short rows pad.",
        ),
        Case(
            "fill_pptx_table/on-a-p-sp", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/sp_table.pptx",
                "slide_index": 0, "shape_name": "tablePlaceholderSp",
                "rows": [["H1", "H2"], ["a\nb", "c"]],
            },
            files=PPT,
            projection=ProjectionSpec("pptx_table_shape", "out/sp_table.pptx",
                                      {"slide_index": 0, "shape_name": "tablePlaceholderSp"}),
            note=(
                "The target is an ordinary p:sp, so the graphicFrame is built "
                "from scratch and inherits the sp's name and xfrm. Cell text "
                "splits on \\n into separate paragraphs."
            ),
        ),
        Case(
            "fill_pptx_table/empty-rows", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/x.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder", "rows": [],
            },
            files=PPT, expect="error",
        ),
        Case(
            "fill_pptx_table/style-id", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/styled.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder",
                "rows": [["A", "B"], ["c", "d"]],
                "style_id": "{2D5ABB26-0587-4C30-8999-92F81FD0307C}",
            },
            files=PPT,
            projection=ProjectionSpec("pptx_table_shape", "out/styled.pptx",
                                      {"slide_index": 0, "shape_name": "Table Placeholder"}),
            note=(
                "style_id replaces the text of the existing a:tableStyleId "
                "rather than adding an element. Nothing exercised this "
                "parameter before."
            ),
        ),
        Case(
            "fill_pptx_table/remainder-columns", "fill_pptx_table",
            {
                "path": "template.pptx", "output_path": "out/remainder.pptx",
                "slide_index": 0, "shape_name": "Table Placeholder",
                "rows": [["a", "b", "c", "d", "e", "f", "g"]],
            },
            files=PPT,
            projection=ProjectionSpec("pptx_table_shape", "out/remainder.pptx",
                                      {"slide_index": 0, "shape_name": "Table Placeholder"}),
            note=(
                "Seven columns across 3657600 EMU does not divide evenly, so "
                "the last gridCol absorbs the remainder. Every other table "
                "case used a width that divided exactly, which meant the "
                "remainder rule was asserted by no case at all."
            ),
        ),
        Case(
            "write_pptx/layouts-and-notes", "write_pptx",
            {
                "path": "out/deck.pptx",
                "slides": [
                    {"layout": 0, "title": "Title slide", "notes": "First notes"},
                    {"title": "Bullets", "bullets": ["one", "two"]},
                    {"layout": 1, "title": "Body", "body": "Some body text"},
                ],
            },
            kind="text",
            projection=ProjectionSpec("pptx_package", "out/deck.pptx"),
            note=(
                "Title is placeholder idx 0; body is the placeholder with "
                "idx == 1, gated on len(placeholders) > 1. Notes are added only "
                "when truthy. Returns 'Created {path} with N slides'."
            ),
        ),
        Case(
            "write_pptx/layout-out-of-range", "write_pptx",
            {"path": "out/fallback.pptx", "slides": [{"layout": 99, "title": "Fallback"}]},
            kind="text",
            projection=ProjectionSpec("pptx_package", "out/fallback.pptx"),
            note="An out-of-range layout falls back to 1, not to 0.",
        ),
        Case(
            "write_pptx/slides-is-a-string", "write_pptx",
            {"path": "out/degenerate.pptx", "slides": "ab"},
            kind="text",
            projection=ProjectionSpec("pptx_package", "out/degenerate.pptx"),
            note=(
                "Not an error: a string is iterable, so each character becomes "
                "a slide whose title is that character. Captured because it is "
                "what the Python does, and a port that validates the argument "
                "instead would be a deliberate change rather than an accident."
            ),
        ),
        Case(
            "write_pptx_from_template/two-slides", "write_pptx_from_template",
            {
                "template_path": "template.pptx", "output_path": "out/from_template.pptx",
                "slides": [
                    {"layout": 1, "title": "From template", "body": "Body text"},
                    {"layout": 0, "title": "Second", "notes": "Notes here"},
                ],
            },
            kind="text", files=PPT,
            projection=ProjectionSpec("pptx_package", "out/from_template.pptx"),
            note=(
                "The four fixture slides stay, and the new ones are appended. "
                "The return text says 'from template'."
            ),
        ),
        Case(
            "write_pptx_from_template/layout-fallback", "write_pptx_from_template",
            {
                "template_path": "template.pptx", "output_path": "out/tpl_fallback.pptx",
                "slides": [{"layout": 99, "title": "Fallback"}],
            },
            kind="text", files=PPT,
            projection=ProjectionSpec("pptx_package", "out/tpl_fallback.pptx"),
            note="Only layout_idx >= len falls back; a negative index wraps like Python.",
        ),
        Case(
            "write_pptx_from_template/negative-layout", "write_pptx_from_template",
            {
                "template_path": "template.pptx", "output_path": "out/neg.pptx",
                "slides": [{"layout": -1, "title": "Negative index"}],
            },
            kind="text", files=PPT,
            projection=ProjectionSpec("pptx_package", "out/neg.pptx"),
            note=(
                "Only `layout_idx >= len` falls back (office.py:764), so a "
                "negative index reaches slide_layouts[-1] and wraps like any "
                "Python sequence -- the last layout, not layout 1. The claim "
                "was in a sibling note with no case behind it."
            ),
        ),
        Case(
            "write_pptx_from_template/bad-template", "write_pptx_from_template",
            {
                "template_path": "broken.xlsx", "output_path": "out/x.pptx",
                "slides": [{"title": "x"}],
            },
            kind="text", files=("broken.xlsx",), expect="error",
        ),
    ]


def pdf_cases() -> list[Case]:
    return [
        Case(
            "read_pdf_text/plain", "read_pdf_text", {"path": "note.pdf"}, files=PDF,
            note="layout=false clusters words into lines by top (tol 3), joined by single spaces.",
        ),
        Case(
            "read_pdf_text/layout", "read_pdf_text",
            {"path": "note.pdf", "layout": True}, files=PDF,
            note="pdfplumber to_textmap with x_density 7.25, y_density 13, half-even rounds.",
        ),
        Case(
            "read_pdf_text/rotated", "read_pdf_text", {"path": "rot.pdf"},
            files=("rot.pdf",),
            note="/Rotate 90 must be folded into the CTM before coordinates mean anything.",
        ),
        Case(
            "read_pdf_text/missing", "read_pdf_text", {"path": "nope.pdf"}, files=PDF,
            expect="error",
        ),
        Case(
            "extract_pdf_words/page1", "extract_pdf_words",
            {"path": "note.pdf", "page": 1}, files=PDF,
            note=(
                "pdfminer's LTChar model: adv = width/1000*fs*(Tz/100), bbox "
                "through Tm x CTM, top = page_h - y1. Words split on "
                "whitespace, cx < ax, cx > bx + 3, |dtop| > 3. Coordinates are "
                "2 dp with half-even rounding."
            ),
        ),
        Case(
            "extract_pdf_words/kerned-page", "extract_pdf_words",
            {"path": "note.pdf", "page": 2}, files=PDF,
            note=(
                "TJ kerns, Tc/Tw spacing, Tz 90 horizontal scaling, and both Td "
                "and T* positioning. getTextContent() cannot reproduce these "
                "word boundaries — the operator list has to be walked."
            ),
        ),
        Case(
            "extract_pdf_words/blank-page", "extract_pdf_words",
            {"path": "note.pdf", "page": 3}, files=PDF,
        ),
        Case(
            "extract_pdf_words/page-zero", "extract_pdf_words",
            {"path": "note.pdf", "page": 0}, files=PDF, expect="error",
            note="page is 1-based.",
        ),
        Case(
            "extract_pdf_words/page-past-end", "extract_pdf_words",
            {"path": "note.pdf", "page": 99}, files=PDF, expect="error",
        ),
        Case(
            "extract_pdf_period_table/auto-detect", "extract_pdf_period_table",
            {"path": "note.pdf", "page": 1}, files=PDF,
            note=(
                "The case the skill exists for: Kas/Cash must come back as "
                "[1410, 25641] and never swapped. Bands are the header centres "
                "+/-60 for a single period, else midpoints with an outer margin "
                "of max(min(gap)/2, 20). The first parsed value per band wins; "
                "a dash is 0 only INSIDE a band; label-only rows merge into the "
                "next row within 14 pt, ties going to the next."
            ),
        ),
        Case(
            "extract_pdf_period_table/explicit-periods", "extract_pdf_period_table",
            {"path": "note.pdf", "page": 1, "periods": ["2025", "2024"]}, files=PDF,
        ),
        Case(
            "extract_pdf_period_table/single-period", "extract_pdf_period_table",
            {"path": "note.pdf", "page": 4, "periods": ["2025"]}, files=PDF,
            note=(
                "The single-centre band rule: one period means one band at the "
                "header centre +/-60, rather than midpoints between centres. "
                "`periods` has to be passed explicitly, because auto-detection "
                "needs two year tokens and would never reach this branch -- "
                "see the sibling case."
            ),
        ),
        Case(
            "extract_pdf_period_table/single-period-auto-fails",
            "extract_pdf_period_table",
            {"path": "note.pdf", "page": 4}, files=PDF,
            note=(
                "The same page without `periods`. Auto-detection requires a "
                "header row carrying all the periods or at least two year "
                "tokens (pdf_tables.py:106-108), so a genuine single-period "
                "note is reported as found:false. The reason string does not "
                "mention that passing `periods` would work, which is a gap a "
                "port could close."
            ),
        ),
        Case(
            "extract_pdf_period_table/blank-page", "extract_pdf_period_table",
            {"path": "note.pdf", "page": 3}, files=PDF,
            note="No header row at all: found is false, not an error.",
        ),
    ]


def image_cases() -> list[Case]:
    return [
        Case("image_read/png", "image_read", {"path": "img.png"}, files=("img.png",),
             note="mode is derived for PNG/JPEG/GIF and omitted otherwise."),
        Case("image_read/png-base64", "image_read",
             {"path": "img.png", "include_base64": True}, files=("img.png",),
             note=(
                 "data_url is present because img.png is 76 bytes. The "
                 "1,500,000-byte cap that turns the key into base64_skipped "
                 "has no coverage — no fixture is anywhere near it — so a "
                 "port should treat that branch as unverified."
             )),
        Case("image_read/jpg", "image_read", {"path": "img.jpg"}, files=("img.jpg",),
             note="mimetypes maps .jpg to image/jpeg."),
        Case("image_read/gif", "image_read", {"path": "img.gif"}, files=("img.gif",)),
        Case("image_read/wrong-suffix", "image_read", {"path": "databook.xlsx"},
             files=DB, expect="error", note="Suffix allow-list, checked before opening."),
        Case("image_read/escapes-workspace", "image_read", {"path": "../img.png"},
             files=("img.png",), expect="error",
             note="_resolve_path rejects it; the message names both the resolved and canonical path."),
    ]


def notebook_cases() -> list[Case]:
    return [
        Case(
            "edit_notebook_cell/replace", "edit_notebook_cell",
            {"path": "nb.ipynb", "cell_index": 0, "new_source": "import numpy as np\nx = 1"},
            kind="text", files=("nb.ipynb",),
            projection=ProjectionSpec("notebook", "nb.ipynb"),
            note=(
                "Source is rewritten as [line + '\\n' for line in "
                "splitlines()], which ADDS a trailing newline the input did not "
                "have. Written with indent=2 plus a trailing newline."
            ),
        ),
        Case(
            "edit_notebook_cell/append-past-end", "edit_notebook_cell",
            {"path": "nb.ipynb", "cell_index": 5, "new_source": "appended = True"},
            kind="text", files=("nb.ipynb",),
            projection=ProjectionSpec("notebook", "nb.ipynb"),
            note=(
                "An index may exceed the length by up to 100; the cell is "
                "appended as a code cell with execution_count null."
            ),
        ),
        Case(
            "edit_notebook_cell/negative-index", "edit_notebook_cell",
            {"path": "nb.ipynb", "cell_index": -1, "new_source": "x"},
            kind="text", files=("nb.ipynb",), expect="error",
        ),
    ]


def all_cases(variants: tuple[str, ...]) -> list[Case]:
    cases: list[Case] = []
    cases += excel_cases()
    cases += word_cases()
    cases += pptx_read_cases()
    for variant in variants:
        cases += metrics_cases(variant)
    cases += pptx_write_cases()
    cases += pdf_cases()
    cases += image_cases()
    cases += notebook_cases()
    return cases


def measured_charset(cases: list[Case]) -> str:
    """Every character any case feeds into the text metrics.

    Derived rather than guessed, so the font subsets can never be one glyph
    short of what a case measures.
    """
    chars: set[str] = set()
    for case in cases:
        for value in case.args.values():
            if isinstance(value, str):
                chars.update(value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        chars.update(item)
                    elif isinstance(item, dict):
                        for inner in item.values():
                            if isinstance(inner, str):
                                chars.update(inner)
                    elif isinstance(item, list):
                        for inner in item:
                            if isinstance(inner, str):
                                chars.update(inner)
    return "".join(sorted(chars))
