"""Expose the real office tools over MCP, implementation and boundary intact.

The four thin servers next to this one (excel, docx, pdf, databook) were
written as standalone reimplementations: `analyze_data` computes min/max/mean,
`read_sheet` returns headers and rows. Copilot can already do all of that, so
exposing those to it duplicates what it has and adds nothing.

The tools worth reaching from outside are in `coding_agent/tools/office.py`,
and none of them were exposed: `locate_databook_stage_columns`,
`write_pptx_from_template`, `fill_pptx_table`, `estimate_pptx_text_capacity`
and the bounded-autofit text fitting behind it, `inspect_excel_sheets` with its
hidden row/column/sheet detection, `extract_pdf_period_table`. That is the
firm-specific half -- the part no general assistant can be told to work out.

This server does not reimplement any of it. `office_tools()` already returns
each tool's real schema and a handler bound to a ToolRegistry, so the whole
bridge is a loop. Two things follow for free: there is one implementation to
maintain rather than two, and every path argument goes through
`ToolRegistry._resolve_path`, so a caller cannot read or write outside the
workspace root.

Usage:
    python -m coding_agent.plugins.mcp_servers.office_mcp [workspace_root]

The root defaults to $YUCODE_MCP_ROOT, then to the working directory. In
`.vscode/mcp.json` set it with "cwd", or pass it as an argument.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from ._protocol import McpStdioServer


def _workspace_root(argv: list[str]) -> Path:
    if len(argv) > 1 and argv[1].strip():
        return Path(argv[1]).expanduser().resolve()
    env = os.environ.get("YUCODE_MCP_ROOT", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def build_server(root: Path) -> McpStdioServer:
    from ...config.settings import load_app_config
    from ...tools import ToolRegistry
    from ...tools.office import office_tools

    registry = ToolRegistry(root, load_app_config(None, workspace=root))
    # Same root for both layers. The registry would refuse an escaping path on
    # its own, but leaving the protocol guard on its default (the working
    # directory) means the two disagree about what is in bounds, and a refusal
    # then cites a directory the caller was never told about.
    server = McpStdioServer("yucode-office", root=root)
    for definition in office_tools(registry):
        spec = definition.spec
        server.register_tool(
            spec.name,
            # The permission and risk grades are yucode's own vocabulary and
            # mean nothing to another client, but "writes files" is worth
            # saying plainly to a model that is deciding whether to call this.
            spec.description if spec.permission == "read-only"
            else f"{spec.description} (writes to disk)",
            spec.parameters,
            definition.handler,
        )
    return server


def main() -> None:
    root = _workspace_root(sys.argv)
    if not root.is_dir():
        sys.stderr.write(f"yucode-office: workspace root does not exist: {root}\n")
        raise SystemExit(2)
    # stderr, never stdout: stdout is the JSON-RPC channel and one stray line
    # on it desynchronises the client for the rest of the session.
    sys.stderr.write(f"yucode-office: serving {root}\n")
    build_server(root).serve()


if __name__ == "__main__":
    main()
