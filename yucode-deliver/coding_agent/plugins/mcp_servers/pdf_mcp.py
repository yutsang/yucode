"""MCP server for PDF file operations.

Requires: pdfplumber >= 0.11

Run:  python -m coding_agent.plugins.mcp_servers.pdf_mcp
"""

from __future__ import annotations

from typing import Any

from ._protocol import McpStdioServer


def _require_pdfplumber():
    try:
        import pdfplumber  # noqa: F401
        return pdfplumber
    except ImportError as exc:
        raise RuntimeError("pdfplumber is required: pip install pdfplumber>=0.11") from exc


def handle_extract_text(args: dict[str, Any]) -> Any:
    pdfplumber = _require_pdfplumber()
    path = args["path"]
    pages_arg = args.get("pages")
    max_chars = int(args.get("max_chars", 100000))

    with pdfplumber.open(path) as pdf:
        total_pages = len(pdf.pages)
        if pages_arg:
            page_indices = [int(p) - 1 for p in (pages_arg if isinstance(pages_arg, list) else [pages_arg])]
        else:
            page_indices = list(range(total_pages))

        result = []
        char_count = 0
        for idx in page_indices:
            if idx < 0 or idx >= total_pages:
                continue
            text = pdf.pages[idx].extract_text() or ""
            if char_count + len(text) > max_chars:
                text = text[:max_chars - char_count]
                result.append({"page": idx + 1, "text": text, "truncated": True})
                break
            result.append({"page": idx + 1, "text": text})
            char_count += len(text)

    return {"total_pages": total_pages, "pages": result}


def handle_metadata(args: dict[str, Any]) -> Any:
    pdfplumber = _require_pdfplumber()
    with pdfplumber.open(args["path"]) as pdf:
        meta = pdf.metadata or {}
        return {
            "total_pages": len(pdf.pages),
            "metadata": {k: str(v) for k, v in meta.items()},
        }


def handle_extract_tables(args: dict[str, Any]) -> Any:
    pdfplumber = _require_pdfplumber()
    page_num = int(args.get("page", 1))

    with pdfplumber.open(args["path"]) as pdf:
        if page_num < 1 or page_num > len(pdf.pages):
            raise ValueError(f"Page {page_num} out of range (1-{len(pdf.pages)})")
        tables = pdf.pages[page_num - 1].extract_tables() or []
        result = []
        for t_idx, table in enumerate(tables):
            rows = []
            for row in table:
                rows.append([str(c) if c is not None else "" for c in row])
            result.append({"index": t_idx, "rows": rows})
    return {"page": page_num, "tables": result}


def handle_page_count(args: dict[str, Any]) -> Any:
    pdfplumber = _require_pdfplumber()
    with pdfplumber.open(args["path"]) as pdf:
        return {"total_pages": len(pdf.pages)}


def main():
    server = McpStdioServer("yucode-pdf")

    server.register_tool("extract_text", "Extract text from PDF pages.", {
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "Path to PDF file"},
            "pages": {
                "oneOf": [{"type": "integer"}, {"type": "array", "items": {"type": "integer"}}],
                "description": "Page number(s), 1-indexed. Omit for all pages.",
            },
            "max_chars": {"type": "integer", "description": "Max total characters (default 100000)"},
        },
        "required": ["path"],
    }, handle_extract_text)

    server.register_tool("metadata", "Get PDF metadata and page count.", {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }, handle_metadata)

    server.register_tool("extract_tables", "Extract tables from a specific PDF page.", {
        "type": "object",
        "properties": {
            "path": {"type": "string"},
            "page": {"type": "integer", "description": "Page number, 1-indexed (default 1)"},
        },
        "required": ["path"],
    }, handle_extract_tables)

    server.register_tool("page_count", "Get the total page count of a PDF.", {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }, handle_page_count)

    server.serve()


if __name__ == "__main__":
    main()
