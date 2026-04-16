"""Built-in Office file tools -- Word (.docx), Excel (.xlsx), PowerPoint (.pptx).

These are direct tools (no MCP server needed). Each gracefully degrades
if the optional dependency is missing, telling the model how to install it.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from . import RiskLevel, ToolDefinition, ToolSpec

if TYPE_CHECKING:
    from . import ToolRegistry


def office_tools(registry: ToolRegistry) -> list[ToolDefinition]:
    return [
        # ---- Excel ----
        ToolDefinition(
            ToolSpec(
                "read_excel_sheet",
                "Read an Excel (.xlsx) sheet as headers + rows. Returns JSON with 'headers' and 'rows'.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .xlsx file."},
                        "sheet": {"type": "string", "description": "Sheet name (default: active sheet)."},
                        "max_rows": {"type": "integer", "description": "Max data rows to return (default 500)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_excel_sheet(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "list_excel_sheets",
                "List visible sheet names in an Excel (.xlsx) file. Hidden sheets are excluded.",
                {
                    "type": "object",
                    "properties": {"path": {"type": "string", "description": "Path to .xlsx file."}},
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _list_excel_sheets(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "inspect_excel_sheets",
                "Scan all visible sheets in an Excel file. Returns each sheet's name, "
                "estimated row count, column headers, and sample rows. "
                "Call this before read_excel_sheet when you need to identify which sheet "
                "contains the data the user is looking for.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .xlsx file."},
                        "sample_rows": {"type": "integer", "description": "Data rows to sample per sheet (default 3)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _inspect_excel_sheets(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "write_excel_cell",
                "Write a value to a cell in an Excel (.xlsx) file.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sheet": {"type": "string", "description": "Sheet name (default: active)."},
                        "cell": {"type": "string", "description": "Cell reference like A1, B2."},
                        "value": {"type": "string", "description": "Value to write."},
                    },
                    "required": ["path", "cell", "value"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _write_excel_cell(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "excel_to_json",
                "Convert an Excel sheet to a list of JSON records (using first row as headers).",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "sheet": {"type": "string"},
                        "max_rows": {"type": "integer", "description": "Max rows (default 500)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _excel_to_json(registry, args),
        ),
        # ---- Word ----
        ToolDefinition(
            ToolSpec(
                "read_word_text",
                "Read the full text of a Word (.docx) file.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .docx file."},
                        "max_chars": {"type": "integer", "description": "Max characters (default 50000)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_word_text(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "read_word_paragraphs",
                "Read paragraphs from a Word (.docx) file with style info.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "max_paragraphs": {"type": "integer", "description": "Max paragraphs (default 1000)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_word_paragraphs(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "write_word",
                "Create a new Word (.docx) file with paragraphs.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "paragraphs": {
                            "type": "array",
                            "description": "List of paragraphs. Each can be a string or {text, style}.",
                            "items": {},
                        },
                    },
                    "required": ["path", "paragraphs"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _write_word(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "append_word",
                "Append a paragraph to an existing Word (.docx) file.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "text": {"type": "string"},
                        "style": {"type": "string", "description": "Paragraph style (default Normal)."},
                    },
                    "required": ["path", "text"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _append_word(registry, args),
        ),
        # ---- PowerPoint ----
        ToolDefinition(
            ToolSpec(
                "read_pptx",
                "Read text content from a PowerPoint (.pptx) file. Returns slide-by-slide text.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .pptx file."},
                        "max_slides": {"type": "integer", "description": "Max slides to read (default 100)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_pptx(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "write_pptx",
                "Create a new PowerPoint (.pptx) file with slides. Each slide has a title and body.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "slides": {
                            "type": "array",
                            "description": "List of slides, each {title, body} or {title, bullets: [...]}.",
                            "items": {},
                        },
                    },
                    "required": ["path", "slides"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _write_pptx(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "write_pptx_from_template",
                "Create a PowerPoint (.pptx) from an existing template. "
                "Replaces placeholders in the template and adds slides with data.",
                {
                    "type": "object",
                    "properties": {
                        "template_path": {"type": "string", "description": "Path to template .pptx file."},
                        "output_path": {"type": "string", "description": "Output .pptx file path."},
                        "slides": {
                            "type": "array",
                            "description": "List of slides: each {title, body, bullets: [...], notes: str}.",
                            "items": {},
                        },
                    },
                    "required": ["template_path", "output_path", "slides"],
                },
                "workspace-write",
                RiskLevel.MEDIUM,
            ),
            lambda args: _write_pptx_from_template(registry, args),
        ),
        ToolDefinition(
            ToolSpec(
                "read_excel_preview",
                "Read an Excel file with schema inference. Returns headers, types, and a preview of the data.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .xlsx file."},
                        "sheet": {"type": "string", "description": "Sheet name (default: active sheet)."},
                        "max_rows": {"type": "integer", "description": "Max preview rows (default 20)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_excel_preview(registry, args),
        ),
        # ---- PDF ----
        ToolDefinition(
            ToolSpec(
                "read_pdf_text",
                "Extract text from a PDF file. Returns page-by-page text.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to .pdf file."},
                        "max_pages": {"type": "integer", "description": "Max pages to read (default 50)."},
                    },
                    "required": ["path"],
                },
                "read-only",
                RiskLevel.LOW,
            ),
            lambda args: _read_pdf_text(registry, args),
        ),
    ]


# ---- Excel handlers ---------------------------------------------------------

def _require(module: str, package: str, pip_extra: str) -> Any:
    try:
        return __import__(module)
    except ImportError as exc:
        raise RuntimeError(
            f"{package} is required for this tool. "
            f"Install with: pip install yucode-agent[{pip_extra}]  or  pip install {package}"
        ) from exc


def _get_visibility_state(ws: Any) -> tuple[set[int], set[int]]:
    """Return (hidden_row_numbers_1based, hidden_col_indices_1based).

    Requires a fully-loaded Worksheet (not ReadOnlyWorksheet) so that
    row_dimensions and column_dimensions are available.
    """
    from openpyxl.utils import column_index_from_string
    hidden_rows: set[int] = set()
    hidden_cols: set[int] = set()
    for row_num, rd in ws.row_dimensions.items():
        if rd.hidden:
            hidden_rows.add(int(row_num))
    for col_letter, cd in ws.column_dimensions.items():
        if cd.hidden:
            try:
                hidden_cols.add(column_index_from_string(col_letter))
            except Exception:
                pass
    return hidden_rows, hidden_cols


def _read_excel_sheet(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path), data_only=True)
    sheet_name = args.get("sheet")
    ws = wb[sheet_name] if sheet_name else wb.active
    max_rows = int(args.get("max_rows", 500))
    hidden_rows, hidden_cols = _get_visibility_state(ws)
    rows: list[list[str]] = []
    skipped = 0
    for row_cells in ws.iter_rows():
        if not row_cells:
            continue
        row_num = row_cells[0].row
        if row_num in hidden_rows:
            skipped += 1
            continue
        if len(rows) >= max_rows + 1:
            break
        rows.append([
            str(c.value) if c.value is not None else ""
            for c in row_cells if c.column not in hidden_cols
        ])
    wb.close()
    if not rows:
        return json.dumps({"headers": [], "rows": []})
    result: dict[str, Any] = {"headers": rows[0], "rows": rows[1:]}
    if skipped or hidden_cols:
        result["note"] = f"Skipped {skipped} hidden row(s) and {len(hidden_cols)} hidden column(s)."
    return json.dumps(result)


def _list_excel_sheets(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path), read_only=True)
    visible = [ws.title for ws in wb.worksheets if ws.sheet_state == "visible"]
    hidden_count = sum(1 for ws in wb.worksheets if ws.sheet_state != "visible")
    wb.close()
    result: dict[str, Any] = {"sheets": visible}
    if hidden_count:
        result["hidden_sheets_count"] = hidden_count
    return json.dumps(result)


def _write_excel_cell(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path))
    sheet_name = args.get("sheet")
    ws = wb[sheet_name] if sheet_name else wb.active
    cell = args["cell"]
    ws[cell] = args["value"]
    wb.save(str(path))
    wb.close()
    return f"Wrote '{args['value']}' to {cell} in {path.name}"


def _excel_to_json(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path), data_only=True)
    sheet_name = args.get("sheet")
    ws = wb[sheet_name] if sheet_name else wb.active
    max_rows = int(args.get("max_rows", 500))
    hidden_rows, hidden_cols = _get_visibility_state(ws)
    rows: list[list[str]] = []
    for row_cells in ws.iter_rows():
        if not row_cells:
            continue
        if row_cells[0].row in hidden_rows:
            continue
        if len(rows) >= max_rows + 1:
            break
        rows.append([
            str(c.value) if c.value is not None else ""
            for c in row_cells if c.column not in hidden_cols
        ])
    wb.close()
    if not rows:
        return "[]"
    headers = [str(h) if h else f"col_{i}" for i, h in enumerate(rows[0])]
    result = []
    for row in rows[1:]:
        result.append({
            headers[j] if j < len(headers) else f"col_{j}": v
            for j, v in enumerate(row)
        })
    return json.dumps(result, indent=2)


# ---- Word handlers ----------------------------------------------------------

def _read_word_text(registry: ToolRegistry, args: dict[str, Any]) -> str:
    docx = _require("docx", "python-docx>=1.1", "word")
    path = registry._resolve_path(str(args["path"]))
    doc = docx.Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs)
    max_chars = int(args.get("max_chars", 50000))
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... truncated at {max_chars} chars"
    return text


def _read_word_paragraphs(registry: ToolRegistry, args: dict[str, Any]) -> str:
    docx = _require("docx", "python-docx>=1.1", "word")
    path = registry._resolve_path(str(args["path"]))
    doc = docx.Document(str(path))
    max_paras = int(args.get("max_paragraphs", 1000))
    paragraphs = []
    for i, para in enumerate(doc.paragraphs):
        if i >= max_paras:
            break
        paragraphs.append({"index": i, "style": para.style.name, "text": para.text})
    return json.dumps({"paragraphs": paragraphs, "total": len(doc.paragraphs)})


def _write_word(registry: ToolRegistry, args: dict[str, Any]) -> str:
    docx = _require("docx", "python-docx>=1.1", "word")
    path = registry._resolve_path(str(args["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = docx.Document()
    for para in args.get("paragraphs", []):
        text = para if isinstance(para, str) else str(para.get("text", ""))
        style = "Normal" if isinstance(para, str) else str(para.get("style", "Normal"))
        doc.add_paragraph(text, style=style)
    doc.save(str(path))
    return f"Created {path}"


def _append_word(registry: ToolRegistry, args: dict[str, Any]) -> str:
    docx = _require("docx", "python-docx>=1.1", "word")
    path = registry._resolve_path(str(args["path"]))
    doc = docx.Document(str(path))
    style = args.get("style", "Normal")
    doc.add_paragraph(args["text"], style=style)
    doc.save(str(path))
    return f"Appended paragraph to {path}"


# ---- PowerPoint handlers ----------------------------------------------------

def _read_pptx(registry: ToolRegistry, args: dict[str, Any]) -> str:
    pptx = _require("pptx", "python-pptx>=1.0", "pptx")
    path = registry._resolve_path(str(args["path"]))
    prs = pptx.Presentation(str(path))
    max_slides = int(args.get("max_slides", 100))
    slides = []
    for i, slide in enumerate(prs.slides):
        if i >= max_slides:
            break
        texts = []
        for shape in slide.shapes:
            if hasattr(shape, "text") and shape.text.strip():
                texts.append(shape.text.strip())
        slides.append({"slide": i + 1, "text": "\n".join(texts)})
    return json.dumps({"slides": slides, "total": len(prs.slides)})


def _write_pptx(registry: ToolRegistry, args: dict[str, Any]) -> str:
    pptx = _require("pptx", "python-pptx>=1.0", "pptx")
    path = registry._resolve_path(str(args["path"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    prs = pptx.Presentation()
    for slide_data in args.get("slides", []):
        if isinstance(slide_data, str):
            layout = prs.slide_layouts[1]
            slide = prs.slides.add_slide(layout)
            slide.shapes.title.text = slide_data
            continue
        title = slide_data.get("title", "")
        body = slide_data.get("body", "")
        bullets = slide_data.get("bullets", [])
        layout = prs.slide_layouts[1]
        slide = prs.slides.add_slide(layout)
        if title and slide.shapes.title:
            slide.shapes.title.text = title
        if slide.placeholders and len(slide.placeholders) > 1:
            tf = slide.placeholders[1].text_frame
            if body:
                tf.text = body
            for bullet in bullets:
                p = tf.add_paragraph()
                p.text = str(bullet)
    prs.save(str(path))
    return f"Created {path} with {len(args.get('slides', []))} slides"


# ---- Template PPTX handler --------------------------------------------------

def _write_pptx_from_template(registry: ToolRegistry, args: dict[str, Any]) -> str:
    pptx = _require("pptx", "python-pptx>=1.0", "pptx")
    template_path = registry._resolve_path(str(args["template_path"]))
    output_path = registry._resolve_path(str(args["output_path"]))
    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs = pptx.Presentation(str(template_path))
    for slide_data in args.get("slides", []):
        layout_idx = int(slide_data.get("layout", 1))
        if layout_idx >= len(prs.slide_layouts):
            layout_idx = 1
        layout = prs.slide_layouts[layout_idx]
        slide = prs.slides.add_slide(layout)
        title = slide_data.get("title", "")
        if title and slide.shapes.title:
            slide.shapes.title.text = title
        body = slide_data.get("body", "")
        bullets = slide_data.get("bullets", [])
        if slide.placeholders and len(slide.placeholders) > 1:
            tf = slide.placeholders[1].text_frame
            if body:
                tf.text = body
            for bullet in bullets:
                p = tf.add_paragraph()
                p.text = str(bullet)
        notes = slide_data.get("notes", "")
        if notes and hasattr(slide, "notes_slide"):
            slide.notes_slide.notes_text_frame.text = notes
    prs.save(str(output_path))
    return f"Created {output_path} from template with {len(args.get('slides', []))} slides"


# ---- Excel preview handler --------------------------------------------------

def _read_excel_preview(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path), data_only=True)
    sheet_name = args.get("sheet")
    ws = wb[sheet_name] if sheet_name else wb.active
    max_rows = int(args.get("max_rows", 20))
    hidden_rows, hidden_cols = _get_visibility_state(ws)
    rows: list[list[str]] = []
    for row_cells in ws.iter_rows():
        if not row_cells:
            continue
        if row_cells[0].row in hidden_rows:
            continue
        if len(rows) >= max_rows + 1:
            break
        rows.append([
            str(c.value) if c.value is not None else ""
            for c in row_cells if c.column not in hidden_cols
        ])
    wb.close()
    if not rows:
        return json.dumps({"headers": [], "rows": [], "schema": []})
    headers = rows[0]
    data = rows[1:]
    schema = []
    for col_idx, h in enumerate(headers):
        values = [r[col_idx] for r in data[:20] if col_idx < len(r) and r[col_idx]]
        numeric_count = sum(1 for v in values if _is_numeric(v))
        inferred = "numeric" if numeric_count > len(values) * 0.7 else "text"
        schema.append({"name": h, "inferred_type": inferred})
    return json.dumps({
        "headers": headers,
        "row_count": len(data),
        "truncated": len(data) >= max_rows,
        "schema": schema,
        "preview": data[:10],
    })


def _inspect_excel_sheets(registry: ToolRegistry, args: dict[str, Any]) -> str:
    openpyxl = _require("openpyxl", "openpyxl>=3.1", "excel")
    path = registry._resolve_path(str(args["path"]))
    wb = openpyxl.load_workbook(str(path), data_only=True)
    sample_rows = int(args.get("sample_rows", 3))
    sheets_info = []
    hidden_count = 0
    for ws in wb.worksheets:
        if ws.sheet_state != "visible":
            hidden_count += 1
            continue
        hidden_rows, hidden_cols = _get_visibility_state(ws)
        rows: list[list[str]] = []
        for row_cells in ws.iter_rows():
            if not row_cells:
                continue
            if row_cells[0].row in hidden_rows:
                continue
            rows.append([
                str(c.value) if c.value is not None else ""
                for c in row_cells if c.column not in hidden_cols
            ])
            if len(rows) >= sample_rows + 1:
                break
        headers = rows[0] if rows else []
        samples = rows[1:] if len(rows) > 1 else []
        total = max(0, (ws.max_row or 1) - 1 - len(hidden_rows))
        entry: dict[str, Any] = {
            "name": ws.title,
            "estimated_data_rows": total,
            "headers": headers,
            "sample": samples,
        }
        if hidden_rows:
            entry["hidden_rows"] = len(hidden_rows)
        if hidden_cols:
            entry["hidden_cols"] = len(hidden_cols)
        sheets_info.append(entry)
    wb.close()
    result: dict[str, Any] = {"sheets": sheets_info}
    if hidden_count:
        result["hidden_sheets"] = hidden_count
    return json.dumps(result, indent=2)


def _is_numeric(value: str) -> bool:
    try:
        float(value.replace(",", ""))
        return True
    except (ValueError, AttributeError):
        return False


# ---- PDF handler ------------------------------------------------------------

def _read_pdf_text(registry: ToolRegistry, args: dict[str, Any]) -> str:
    pdfplumber = _require("pdfplumber", "pdfplumber>=0.10", "pdf")
    path = registry._resolve_path(str(args["path"]))
    max_pages = int(args.get("max_pages", 50))
    pages = []
    with pdfplumber.open(str(path)) as pdf:
        for i, page in enumerate(pdf.pages):
            if i >= max_pages:
                break
            text = page.extract_text() or ""
            pages.append({"page": i + 1, "text": text})
    return json.dumps({"pages": pages, "total_pages": len(pages)})
