"""Deterministic synthetic fixtures for the office golden suite.

Every fixture is built from bytes this module controls, so the same inputs
produce the same file on any machine and in any year. That property is the
whole point: an oracle whose inputs drift cannot tell you whether the
TypeScript port broke or the fixture did.

Two deliberate choices worth knowing before editing anything here.

**`databook.xlsx` is hand-assembled OOXML, not openpyxl output.** Four of the
features the Excel tools have to be tested against are unreachable through
openpyxl, each verified against 3.1.2:

  * a formula's cached value comes out as `<f>…</f><v></v>` — the `<v>` is
    always empty, so `data_only=True` can never see a number;
  * `CellRichText` round-trips as `t="inlineStr"` and never reaches
    `xl/sharedStrings.xml` (openpyxl writes no shared-strings part at all);
  * floats go through `"%.16g"`, so `0.1 + 0.2` is written as `0.3` and
    `0.30000000000000004` cannot be expressed;
  * `column_dimensions.group("C", "E", hidden=True)` also injects
    `outlineLevel="1"`, which is signal the fixture should not carry.

The repo already has a precedent for writing a format by hand rather than
reaching for a library that cannot say what you need — `_make_pdf` in
tests/test_pdf_tables.py. `make_pdf` below is that function, generalised.

**Zip timestamps are canonicalised.** openpyxl, python-docx and python-pptx
all write through `ZipFile.writestr`, which stamps `time.localtime()`. Three
saves two seconds apart give three different files. `canonical_zip_bytes`
rewrites every member with a fixed DOS date and fixed compression, so a
fixture's bytes change only when its content does — which is what makes the
committed binaries reviewable by reconstruction.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass
from typing import Callable

# DOS epoch. Any fixed value works; this one is the conventional "no date".
ZIP_DATE_TIME = (1980, 1, 1, 0, 0, 0)

XL_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"


# --------------------------------------------------------------------------
# determinism
# --------------------------------------------------------------------------


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


# A fixed instant for every docProps timestamp. python-docx and python-pptx
# inherit frozen dates from their bundled templates, but openpyxl stamps
# `now()` — and python-pptx builds a chart's backing workbook through openpyxl,
# so a deck containing a chart carries a clock two layers down.
_FROZEN_INSTANT = "2020-01-01T00:00:00Z"
_DCTERMS_RE = re.compile(
    rb"(<dcterms:(?:created|modified)[^>]*>)[^<]*(</dcterms:(?:created|modified)>)"
)

_NESTED_OOXML = (".xlsx", ".docx", ".pptx")


def freeze_docprops(raw: bytes) -> bytes:
    return _DCTERMS_RE.sub(rb"\g<1>" + _FROZEN_INSTANT.encode() + rb"\g<2>", raw)


def canonical_zip_bytes(raw: bytes) -> bytes:
    """Rewrite an OOXML package with fixed timestamps, order and permissions.

    `[Content_Types].xml` stays first because some readers expect it there;
    everything else is sorted so the member order cannot depend on the order a
    library happened to add parts in.

    Two things are rewritten inside members rather than around them.
    `docProps/core.xml` has its dcterms dates frozen, and any nested OOXML
    package — a chart's embedded workbook is the one that actually occurs — is
    canonicalised recursively. Without the recursion a deck with a chart gets a
    new sha256 every second, which is how this was found.
    """
    source = zipfile.ZipFile(io.BytesIO(raw))
    names = source.namelist()
    ordered = [n for n in names if n == "[Content_Types].xml"]
    ordered += sorted(n for n in names if n != "[Content_Types].xml")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as dest:
        for name in ordered:
            payload = source.read(name)
            if name.endswith("core.xml"):
                payload = freeze_docprops(payload)
            elif name.endswith(_NESTED_OOXML) and payload[:4] == b"PK\x03\x04":
                payload = canonical_zip_bytes(payload)
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            dest.writestr(info, payload)
    return out.getvalue()


def assert_deterministic(name: str, build: Callable[[], bytes]) -> bytes:
    """Build twice and fail loudly if the two differ.

    This is the guard for a future library upgrade that starts writing a uuid
    or a timestamp: without it, the nondeterminism lands silently in a
    committed fixture and every later `--check` reports drift that has nothing
    to do with the port.
    """
    first = build()
    second = build()
    if first[:4] == b"PK\x03\x04":
        first, second = canonical_zip_bytes(first), canonical_zip_bytes(second)
    if first != second:
        raise SystemExit(
            f"fixture `{name}` is not deterministic: two builds differ "
            f"({sha256(first)[:12]} vs {sha256(second)[:12]})"
        )
    return first


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class PdfPage:
    """One page: a raw content stream plus an optional /Rotate."""

    content: str
    rotate: int = 0


def pdf_text_ops(items: list[tuple[float, float, str]], size: float = 8) -> str:
    """Build a plain `Tm … Tj` content stream. y is measured from the bottom."""
    lines = ["BT", f"/F1 {size:g} Tf"]
    for x, y, text in items:
        escaped = text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        lines.append(f"1 0 0 1 {x:g} {y:g} Tm ({escaped}) Tj")
    lines.append("ET")
    return "\n".join(lines)


def make_pdf(pages: list[PdfPage]) -> bytes:
    """Assemble a multi-page PDF from raw content streams, stdlib only.

    Generalised from tests/test_pdf_tables.py:23-53, which built exactly one
    page. Object numbering is fixed by construction and every offset is a
    function of the content, so the output is byte-stable.

    Object layout: 1 catalog, 2 pages, 3 font, then for each page a page
    object followed by its content stream.
    """
    objects: list[bytes] = []

    page_obj_numbers = [4 + 2 * i for i in range(len(pages))]
    kids = " ".join(f"{n} 0 R" for n in page_obj_numbers)

    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode("latin-1")
    )
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    for index, page in enumerate(pages):
        stream = page.content.encode("latin-1")
        content_obj = page_obj_numbers[index] + 1
        rotate = f" /Rotate {page.rotate}" if page.rotate else ""
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792]{rotate} "
                f"/Contents {content_obj} 0 R "
                f"/Resources << /Font << /F1 3 0 R >> >> >>"
            ).encode("latin-1")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for number, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode()
    return bytes(out)


# The bilingual cash note from tests/test_pdf_tables.py:59-77, kept verbatim so
# the golden and the existing Python test describe the same page. Header
# x-centres ~340 (2025) and ~440 (2024); numbers right-aligned so their text
# starts left of the header centre; right-hand translated labels at x>=505,
# outside the last band's right edge (440 + 50 = 490).
BILINGUAL_NOTE: list[tuple[float, float, str]] = [
    (60, 720, "4."), (75, 720, "Kas dan setara kas - neto"),
    (330, 700, "2025"), (430, 700, "2024"),
    (60, 680, "Kas"), (322, 680, "1,410"), (415, 680, "25,641"), (505, 680, "Cash"),
    (60, 665, "PT Bank HSBC Indonesia - Rupiah"),
    (295, 665, "17,948,575"), (398, 665, "4,697,031"), (505, 665, "Bank Rupiah"),
    # Wrapped label: the first line has no values, they sit on the second.
    (60, 650, "PT Bank Maybank Indonesia,"),
    (60, 638, "Tbk. - Rupiah deposit"),
    (290, 638, "92,000,000"), (390, 638, "100,000,000"), (505, 638, "Deposito Rupiah"),
    # Dash under 2025 only; a real value under 2024.
    (60, 620, "PT Bank OCBC NISP, Tbk."), (338, 620, "-"), (390, 620, "47,000,000"),
    (505, 620, "Deposito"),
    # Bracketed negatives.
    (60, 600, "Allowance for impairment losses"),
    (318, 600, "(4,983)"), (418, 600, "(8,741)"), (505, 600, "Penyisihan"),
    (60, 580, "Cash and cash equivalents - net"),
    (280, 580, "232,335,365"), (380, 580, "255,845,815"), (505, 580, "Neto"),
]

# Page 2 exercises the text-state machine rather than the table logic:
# TJ arrays with kerns, character and word spacing, horizontal scaling, and
# both Td and T* positioning. A port that reads getTextContent() instead of
# running the operator list gets the word boundaries here wrong.
_KERNED_PAGE = "\n".join([
    "BT",
    "/F1 10 Tf",
    "14 TL",                       # leading, so T* moves a full line down
    "1 0 0 1 72 700 Tm",
    "[(A) -200 (W) 120 (kerned) -500 (line)] TJ",
    "2 Tc 4 Tw",
    "0 -14 Td",
    "(spaced out text) Tj",
    "0 Tc 0 Tw",
    "90 Tz",
    "T*",
    "(narrow horizontal scaling) Tj",
    "100 Tz",
    "ET",
])

# One period only: the header detector needs a case where there is a single
# year token rather than the two it normally keys on.
_SINGLE_PERIOD_PAGE = pdf_text_ops([
    (60, 720, "3."), (75, 720, "Trade receivables"),
    (330, 700, "2025"),
    (60, 680, "Third parties"), (300, 680, "12,345"),
    (60, 665, "Related parties"), (305, 665, "6,789"),
])


def build_note_pdf() -> bytes:
    return make_pdf([
        PdfPage(pdf_text_ops(BILINGUAL_NOTE)),
        PdfPage(_KERNED_PAGE),
        PdfPage(pdf_text_ops([])),            # deliberately blank
        PdfPage(_SINGLE_PERIOD_PAGE),
    ])


def build_rot_pdf() -> bytes:
    """The same note on a /Rotate 90 page.

    The rotation has to be folded into the CTM before coordinates mean
    anything; a port that ignores /Rotate produces plausible-looking output
    with x and y swapped, which no other case would catch.
    """
    return make_pdf([PdfPage(pdf_text_ops(BILINGUAL_NOTE), rotate=90)])


# --------------------------------------------------------------------------
# databook.xlsx — hand-assembled OOXML (see the module docstring)
# --------------------------------------------------------------------------

# Shared strings, referenced by index from the sheets below. Entry 6 is rich
# text: two runs in one <si>, which the readers must concatenate.
_SHARED_STRINGS: list[str] = [
    "Account",
    "Opening",
    "Closing",
    "Notes",
    "Cash at bank",
    "Trade receivables",
    "<RICH>",          # placeholder, expanded in _shared_strings_xml
    "示意性调整数",
    "Mgt acc",
    "Indicative adjusted",
    "CNY'000",
    "2019-12-31",
    "Numbers",
    "Extremes",
]
_RICH_INDEX = 6
_RICH_TEXT = "Bold" + "Plain"   # what a reader must see for entry 6


def _shared_strings_xml() -> bytes:
    items: list[str] = []
    for index, value in enumerate(_SHARED_STRINGS):
        if index == _RICH_INDEX:
            items.append(
                "<si>"
                '<r><rPr><b/></rPr><t>Bold</t></r>'
                "<r><t>Plain</t></r>"
                "</si>"
            )
        else:
            items.append(f"<si><t>{value}</t></si>")
    body = "".join(items)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<sst xmlns="{XL_NS}" count="{len(items)}" uniqueCount="{len(items)}">'
        f"{body}</sst>"
    ).encode("utf-8")


def _styles_xml() -> bytes:
    """A minimal styles part carrying the four number formats under test.

    cellXfs indices, referenced as `s="n"` from the sheets:
      0  General
      1  numFmtId 14   m/d/yyyy          -> a real datetime
      2  numFmtId 31   yyyy"年"m"月"d"日"  -> NOT in openpyxl's BUILTIN_FORMATS,
                                            so the cell reports "General" and
                                            the value stays a raw serial
      3  numFmtId 176  a custom CJK date -> declared here, so it converts
      4  numFmtId 22   m/d/yyyy h:mm     -> datetime with a time part
      5  numFmtId 46   [h]:mm:ss         -> a timedelta
    """
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<styleSheet xmlns="{XL_NS}">'
        '<numFmts count="1">'
        '<numFmt numFmtId="176" formatCode="yyyy&quot;年&quot;m&quot;月&quot;d&quot;日&quot;"/>'
        "</numFmts>"
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="6">'
        '<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>'
        '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="31" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="176" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="22" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        '<xf numFmtId="46" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>'
        "</cellXfs>"
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>'
        "</styleSheet>"
    ).encode("utf-8")


def _cell(ref: str, *, style: int | None = None, kind: str | None = None,
          value: str | None = None, formula: str | None = None,
          inline: str | None = None) -> str:
    attrs = f' r="{ref}"'
    if style is not None:
        attrs += f' s="{style}"'
    if kind is not None:
        attrs += f' t="{kind}"'
    body = ""
    if formula is not None:
        body += f"<f>{formula}</f>"
    if inline is not None:
        body += f"<is><t>{inline}</t></is>"
    if value is not None:
        body += f"<v>{value}</v>"
    return f"<c{attrs}>{body}</c>" if body else f"<c{attrs}/>"


def _sheet_data_xml(rows: list[tuple[int, str, dict]]) -> str:
    parts: list[str] = []
    for index, cells, attrs in rows:
        attr_text = "".join(f' {k}="{v}"' for k, v in attrs.items())
        parts.append(f'<row r="{index}"{attr_text}>{cells}</row>')
    return "".join(parts)


def _sheet_data() -> bytes:
    """The main sheet. Row 4 is hidden; columns C..E are a hidden range.

    Cell contents are chosen so that every str() parity case the port has to
    reproduce appears exactly once: True, 100.0, 1e-07, 1e+16,
    0.30000000000000004, a datetime, a time-bearing datetime, a timedelta, an
    error literal, an inline string, and a rich-text shared string.
    """
    # Column layout is deliberate. B is a single hidden column and C..E are one
    # hidden range, which openpyxl collapses so that only C is actually hidden
    # — so D and E stay VISIBLE in the tool's output, and G/H sit outside the
    # range entirely. Every value whose str() the port has to reproduce is in a
    # visible column; the hidden ones carry filler.
    rows: list[tuple[int, str, dict]] = [
        (1, (
            _cell("A1", kind="s", value="0")      # Account
            + _cell("B1", kind="s", value="1")    # Opening   (hidden)
            + _cell("C1", kind="s", value="2")    # Closing   (hidden)
            + _cell("D1", kind="s", value="3")    # Notes
            + _cell("E1", kind="s", value="11")   # 2019-12-31
            + _cell("F1", kind="s", value="7")    # 示意性调整数
            + _cell("G1", kind="s", value="12")   # Numbers
            + _cell("H1", kind="s", value="13")   # Extremes
        ), {}),
        (2, (
            _cell("A2", kind="s", value="4")
            + _cell("B2", value="1")
            + _cell("C2", value="2")
            + _cell("D2", formula="B2+C2", value="200")   # numeric formula, cached <v>
            + _cell("E2", style=1, value="43830") # 2019-12-31 via numFmt 14
            + _cell("F2", kind="b", value="1")    # -> "True"
            + _cell("G2", value="100")            # -> "100" (int)
            + _cell("H2", value="100.0")          # -> "100.0" (float)
        ), {}),
        (3, (
            _cell("A3", kind="s", value="5")
            + _cell("B3", value="3")
            + _cell("C3", value="4")
            + _cell("D3", value="0.30000000000000004")
            + _cell("E3", style=2, value="43830") # numFmt 31: stays a serial
            + _cell("F3", kind="e", value="#N/A")
            + _cell("G3", value="1E-7")           # -> "1e-07", JS gives "1e-7"
            + _cell("H3", value="1E+16")          # -> "1e+16", JS gives the digits
        ), {}),
        # Hidden row: read_excel_sheet skips it and says so in `note`.
        (4, (
            _cell("A4", kind="inlineStr", inline="hidden row")
            + _cell("B4", value="1")
            + _cell("C4", value="2")
        ), {"hidden": "1"}),
        (5, (
            _cell("A5", kind="inlineStr", inline="inline string")
            + _cell("B5", value="5")
            + _cell("C5", style=3, value="43830")             # custom CJK date
            + _cell("D5", style=4, value="43830.4375")        # 10:30 on that day
            + _cell("E5", style=5, value="1.0833333333333333")  # 1 day, 2:00:00
            + _cell("F5")                                     # style-only, empty
            + _cell("G5", kind="s", value=str(_RICH_INDEX))   # rich text -> BoldPlain
            + _cell("H5", style=3, value="43830")             # custom CJK date, visible
        ), {}),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<worksheet xmlns="{XL_NS}">'
        '<dimension ref="A1:H5"/>'
        "<cols>"
        # A single hidden column, then a hidden RANGE. openpyxl collapses the
        # range into one ColumnDimension keyed "C", which is why office.py
        # hides only column 3 of 3..5. The port fixes that; the golden marks it.
        '<col min="2" max="2" width="12" customWidth="1" hidden="1"/>'
        '<col min="3" max="5" width="12" customWidth="1" hidden="1"/>'
        "</cols>"
        f"<sheetData>{_sheet_data_xml(rows)}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")


def _sheet_cash() -> bytes:
    """A sheet whose used range starts at C3, not A1.

    read_excel_sheet iterates the bounding box, so the header row is the first
    row carrying any <c> at all and the leading blank columns appear as "".
    """
    rows = [
        (3, (
            _cell("C3", kind="inlineStr", inline="Item")
            + _cell("D3", kind="inlineStr", inline="Amount")
        ), {}),
        (4, (
            _cell("C4", kind="inlineStr", inline="Kas")
            + _cell("D4", value="1410")
        ), {}),
        (5, (
            _cell("C5", kind="inlineStr", inline="Bank")
            + _cell("D5", value="25641")
        ), {}),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<worksheet xmlns="{XL_NS}">'
        '<dimension ref="C3:D5"/>'
        f"<sheetData>{_sheet_data_xml(rows)}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")


def _sheet_empty() -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<worksheet xmlns="{XL_NS}"><dimension ref="A1"/><sheetData/></worksheet>'
    ).encode("utf-8")


def _sheet_stages() -> bytes:
    """An FDD databook stage grid, mirroring tests/test_databook_metrics.py:88-98.

    Row 2 carries the dates (as raw serials inside the 32874..54970 window the
    locator accepts), row 3 the basis labels. Row 4 is hidden on purpose: the
    locator scans raw values INCLUDING hidden rows, which is the behaviour a
    port that reuses the visible-row iterator would lose.
    """
    def text(ref: str, value: str) -> str:
        return _cell(ref, kind="inlineStr", inline=value)

    rows = [
        (1, text("A1", "Financials") + text("B1", "CNY'000"), {}),
        (2, (
            text("A2", "")
            + _cell("B2", value="43830") + _cell("C2", value="43830") + _cell("D2", value="43830")
            + _cell("E2", value="43830") + _cell("F2", value="43830") + _cell("G2", value="43830")
        ), {}),
        (3, (
            text("A3", "Account")
            + text("B3", "Mgt acc") + text("C3", "Mgt acc") + text("D3", "Mgt acc")
            + text("E3", "示意性调整数") + text("F3", "示意性调整数") + text("G3", "示意性调整数")
        ), {}),
        (4, (
            text("A4", "Revenue")
            + _cell("B4", value="100") + _cell("C4", value="110") + _cell("D4", value="120")
            + _cell("E4", value="105") + _cell("F4", value="115") + _cell("G4", value="125")
        ), {"hidden": "1"}),
        (5, (
            text("A5", "Cost of sales")
            + _cell("B5", value="-40") + _cell("C5", value="-44") + _cell("D5", value="-48")
        ), {}),
    ]
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<worksheet xmlns="{XL_NS}">'
        '<dimension ref="A1:G5"/>'
        f"<sheetData>{_sheet_data_xml(rows)}</sheetData>"
        "</worksheet>"
    ).encode("utf-8")


_SHEETS: list[tuple[str, str, Callable[[], bytes]]] = [
    # (display name, sheet state, part builder)
    ("Data", "visible", _sheet_data),
    ("Cash", "visible", _sheet_cash),
    ("Empty", "visible", _sheet_empty),
    ("Stages", "visible", _sheet_stages),
    ("HiddenSheet", "hidden", _sheet_empty),
    ("SecretSheet", "veryHidden", _sheet_empty),
]


def build_databook_xlsx() -> bytes:
    parts: dict[str, bytes] = {}

    sheet_entries = []
    overrides = []
    for index, (name, state, builder) in enumerate(_SHEETS, start=1):
        part = f"xl/worksheets/sheet{index}.xml"
        parts[part] = builder()
        state_attr = "" if state == "visible" else f' state="{state}"'
        sheet_entries.append(
            f'<sheet name="{name}" sheetId="{index}" r:id="rId{index}"{state_attr}/>'
        )
        overrides.append(
            f'<Override PartName="/{part}" ContentType="application/vnd.'
            'openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        )

    relationships = "".join(
        f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" '
        f'Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, len(_SHEETS) + 1)
    )
    tail = len(_SHEETS)
    relationships += (
        f'<Relationship Id="rId{tail + 1}" Type="{REL_NS}/sharedStrings" '
        'Target="sharedStrings.xml"/>'
        f'<Relationship Id="rId{tail + 2}" Type="{REL_NS}/styles" Target="styles.xml"/>'
    )

    parts["xl/workbook.xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<workbook xmlns="{XL_NS}" xmlns:r="{REL_NS}">'
        f'<sheets>{"".join(sheet_entries)}</sheets>'
        "</workbook>"
    ).encode("utf-8")
    parts["xl/_rels/workbook.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">{relationships}</Relationships>'
    ).encode("utf-8")
    parts["xl/sharedStrings.xml"] = _shared_strings_xml()
    parts["xl/styles.xml"] = _styles_xml()
    parts["_rels/.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Relationships xmlns="{PKG_REL_NS}">'
        f'<Relationship Id="rId1" Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    ).encode("utf-8")
    parts["[Content_Types].xml"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<Types xmlns="{CT_NS}">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        f'{"".join(overrides)}'
        '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.'
        'openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
        "</Types>"
    ).encode("utf-8")

    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for name in ["[Content_Types].xml"] + sorted(
            n for n in parts if n != "[Content_Types].xml"
        ):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 0
            info.external_attr = 0o600 << 16
            zf.writestr(info, parts[name])
    return out.getvalue()


# --------------------------------------------------------------------------
# notebook
# --------------------------------------------------------------------------


def build_notebook() -> bytes:
    notebook = {
        "cells": [
            {
                "cell_type": "code",
                "execution_count": 1,
                "metadata": {},
                "outputs": [],
                # No trailing newline on the last line: edit_notebook_cell
                # rewrites source as [line + "\n" for line in splitlines()],
                # which silently adds one. That loss is worth a case.
                "source": ["import pandas as pd\n", "df = pd.DataFrame()"],
            },
            {
                "cell_type": "markdown",
                "metadata": {},
                "source": ["# Heading\n", "\n", "Some prose.\n"],
            },
        ],
        "metadata": {"kernelspec": {"display_name": "Python 3", "name": "python3"}},
        "nbformat": 4,
        "nbformat_minor": 5,
    }
    return (json.dumps(notebook, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


# --------------------------------------------------------------------------
# pptx / docx / images — built with the libraries, then patched with lxml
# --------------------------------------------------------------------------

# python-pptx and python-docx inherit a fixed docProps/core.xml from their
# bundled templates (2013-01-27 and 2013-12-23), so nothing in those packages
# carries a clock. Only the zip member timestamps move, which
# canonical_zip_bytes handles.

_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_P = "http://schemas.openxmlformats.org/presentationml/2006/main"


def _qn(tag: str) -> str:
    prefix, local = tag.split(":")
    return "{%s}%s" % ({"a": _A, "p": _P}[prefix], local)


def build_template_pptx(png_bytes: bytes) -> bytes:
    """The deck every pptx case runs against.

    Slide 0 is the house-style shape zoo: the two shapes
    tests/test_office_tools.py:79-94 builds, plus the cases that only appear
    in a real template — a textbox with custom body insets, an autoshape, a
    group, a connector, a picture, a stale `normAutofit fontScale="62500"`
    left over from a previous fill, a paragraph containing `a:br`, and a
    table placeholder that is a `p:sp` rather than a graphicFrame.

    Slide 1 uses layout 1 and leaves its placeholders WITHOUT `a:xfrm`, so
    their position has to be inherited from the layout and then the master.
    House templates rely on that inheritance, and a port that reads only the
    slide gets no dimensions at all.

    Slide 2 is the "Two Content" layout, and slide 3 is the tiny box that
    forces bounded shrink autofit.
    """
    import io as _io

    from pptx import Presentation
    from pptx.util import Emu, Inches, Pt

    prs = Presentation()

    # --- slide 0: the shape zoo, on the blank layout -----------------------
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    textbox = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(2))
    textbox.name = "textMainBullets_L"
    textbox.text_frame.text = "seed"

    table_shape = slide.shapes.add_table(2, 2, Inches(1), Inches(4), Inches(4), Inches(1.5))
    table_shape.name = "Table Placeholder"
    table_shape.table.cell(0, 0).text = "Header A"
    table_shape.table.cell(0, 1).text = "Header B"
    table_shape.table.cell(1, 0).text = "Row A"
    table_shape.table.cell(1, 1).text = "Row B"

    inset = slide.shapes.add_textbox(Inches(5.5), Inches(1), Inches(3), Inches(1.5))
    inset.name = "customInsets"
    body = inset.text_frame._txBody.bodyPr
    # Non-default insets: estimate_pptx_text_capacity subtracts the shape's own
    # bodyPr insets, and a port that always uses the OOXML defaults
    # (91440/91440/45720/45720) gets a different usable width here.
    body.set("lIns", "0")
    body.set("rIns", "0")
    body.set("tIns", "180000")
    body.set("bIns", "180000")

    from pptx.enum.shapes import MSO_CONNECTOR, MSO_SHAPE

    auto = slide.shapes.add_shape(
        MSO_SHAPE.ROUNDED_RECTANGLE, Inches(5.5), Inches(3), Inches(2), Inches(1)
    )
    auto.name = "autoRounded"
    auto.text_frame.text = "auto"

    connector = slide.shapes.add_connector(
        MSO_CONNECTOR.STRAIGHT, Inches(5.5), Inches(4.5), Inches(7), Inches(5)
    )
    connector.name = "connectorLine"

    picture = slide.shapes.add_picture(
        _io.BytesIO(png_bytes), Inches(8.0), Inches(1), Inches(0.5), Inches(0.5)
    )
    picture.name = "pictureShape"

    group = slide.shapes.add_group_shape()
    group.name = "groupShape"
    inner = group.shapes.add_textbox(Inches(8), Inches(3), Inches(1), Inches(0.5))
    inner.name = "groupedText"
    inner.text_frame.text = "in a group"

    stale = slide.shapes.add_textbox(Inches(1), Inches(6), Inches(3), Inches(1))
    stale.name = "staleAutofit"
    stale_body = stale.text_frame._txBody.bodyPr
    # A previous fill left a shrink factor behind. _clear_autofit has to remove
    # BOTH this and the a:spAutoFit that add_textbox ships, which is why the
    # projection records the child count rather than just the winning element.
    normfit = stale_body.makeelement(_qn("a:normAutofit"), {"fontScale": "62500"})
    stale_body.append(normfit)

    with_break = slide.shapes.add_textbox(Inches(4.5), Inches(6), Inches(3), Inches(1))
    with_break.name = "breakText"
    frame = with_break.text_frame
    frame.text = "before"
    run_parent = frame.paragraphs[0]._p
    run_parent.append(run_parent.makeelement(_qn("a:br"), {}))
    tail_run = run_parent.makeelement(_qn("a:r"), {})
    tail_text = tail_run.makeelement(_qn("a:t"), {})
    tail_text.text = "after"
    tail_run.append(tail_text)
    run_parent.append(tail_run)

    # A table placeholder that is an ordinary p:sp. fill_pptx_table has to work
    # on it by building a graphicFrame from scratch, not by editing a table.
    sp_placeholder = slide.shapes.add_textbox(Inches(1), Inches(7.2), Inches(4), Inches(1))
    sp_placeholder.name = "tablePlaceholderSp"

    # The last two entries in the shape_type table. Both are here only so the
    # golden exercises every branch of the mapping a port has to reproduce
    # ("FREEFORM (5)", "CHART (3)"); nothing else reads them.
    freeform_builder = slide.shapes.build_freeform(Inches(6), Inches(6))
    freeform_builder.add_line_segments(
        [(Inches(7), Inches(6)), (Inches(7), Inches(7)), (Inches(6), Inches(7))]
    )
    freeform = freeform_builder.convert_to_shape()
    freeform.name = "freeformShape"

    from pptx.chart.data import CategoryChartData
    from pptx.enum.chart import XL_CHART_TYPE

    chart_data = CategoryChartData()
    chart_data.categories = ["FY24", "FY25"]
    chart_data.add_series("Revenue", (100.0, 120.0))
    chart_frame = slide.shapes.add_chart(
        XL_CHART_TYPE.COLUMN_CLUSTERED,
        Inches(9), Inches(5), Inches(2), Inches(1.5), chart_data,
    )
    chart_frame.name = "chartShape"

    # --- slide 1: inheriting placeholders + speaker notes -------------------
    inheriting = prs.slides.add_slide(prs.slide_layouts[1])
    inheriting.shapes.title.text = "Inherited title"
    inheriting.placeholders[1].text = "Inherited body"
    for shape in inheriting.placeholders:
        # Drop the xfrm python-pptx copied down from the layout, so position
        # has to be resolved by inheritance at read time.
        sp_pr = shape._element.spPr
        xfrm = sp_pr.find(_qn("a:xfrm"))
        if xfrm is not None:
            sp_pr.remove(xfrm)
    inheriting.notes_slide.notes_text_frame.text = "Speaker notes for slide 2."

    # --- slide 2: Two Content ----------------------------------------------
    two = prs.slides.add_slide(prs.slide_layouts[3])
    two.shapes.title.text = "Two content"

    # --- slide 3: the overflow case ----------------------------------------
    tiny_slide = prs.slides.add_slide(prs.slide_layouts[6])
    tiny = tiny_slide.shapes.add_textbox(Inches(1), Inches(1), Inches(2), Emu(int(Pt(21.6) * 12700 / 12700)))
    tiny.width = Inches(2)
    tiny.height = Inches(0.3)
    tiny.name = "tinyBox"

    buffer = _io.BytesIO()
    prs.save(buffer)
    return canonical_zip_bytes(buffer.getvalue())


def build_doc_docx() -> bytes:
    """A document covering every style the Word tools report, plus two traps.

    The trap that matters is the table paragraph: `read_word_text` reads
    `doc.paragraphs`, which is body-level `w:p` only, so a paragraph inside a
    `w:tbl` must NOT appear in the output. A port that walks every `w:p` in
    the document silently includes it.
    """
    import io as _io

    from docx import Document

    doc = Document()
    doc.add_paragraph("Plain body text.")
    doc.add_paragraph("Report title", style="Title")
    doc.add_paragraph("First section", style="Heading 1")
    doc.add_paragraph("A subsection", style="Heading 2")
    doc.add_paragraph("A bullet", style="List Bullet")

    tabbed = doc.add_paragraph()
    run = tabbed.add_run("before")
    run.add_tab()
    run.add_text("after")
    run.add_break()
    run.add_text("second line")

    doc.add_paragraph("Emoji: 📊 and CJK: 現金及現金等價物")

    table = doc.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "inside a table"
    table.cell(0, 1).text = "also inside"

    buffer = _io.BytesIO()
    doc.save(buffer)
    return canonical_zip_bytes(buffer.getvalue())


def build_images() -> dict[str, bytes]:
    """Three tiny images. `image_read` reports mode for PNG/JPEG/GIF only."""
    import io as _io

    from PIL import Image

    out: dict[str, bytes] = {}

    rgba = Image.new("RGBA", (3, 2), (255, 0, 0, 128))
    buffer = _io.BytesIO()
    rgba.save(buffer, format="PNG", optimize=False)
    out["img.png"] = buffer.getvalue()

    rgb = Image.new("RGB", (4, 3), (0, 128, 255))
    buffer = _io.BytesIO()
    rgb.save(buffer, format="JPEG", quality=80)
    out["img.jpg"] = buffer.getvalue()

    palette = Image.new("P", (2, 2))
    buffer = _io.BytesIO()
    palette.save(buffer, format="GIF")
    out["img.gif"] = buffer.getvalue()

    return out


def build_all() -> dict[str, bytes]:
    """Every fixture, keyed by the name it takes inside a case workspace."""
    images = build_images()
    built: dict[str, bytes] = {
        "databook.xlsx": assert_deterministic("databook.xlsx", build_databook_xlsx),
        "note.pdf": assert_deterministic("note.pdf", build_note_pdf),
        "rot.pdf": assert_deterministic("rot.pdf", build_rot_pdf),
        "nb.ipynb": assert_deterministic("nb.ipynb", build_notebook),
        "doc.docx": assert_deterministic("doc.docx", build_doc_docx),
        "template.pptx": assert_deterministic(
            "template.pptx", lambda: build_template_pptx(images["img.png"])
        ),
    }
    built.update(images)
    # A file that is not a zip at all, for the "wrong format" error cases.
    built["broken.xlsx"] = b"not a real xlsx"
    return built
