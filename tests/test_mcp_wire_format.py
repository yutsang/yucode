"""The MCP stdio wire format, pinned in both directions.

Both halves of yucode's MCP support used Content-Length headers -- LSP's
framing, not MCP's. The docstring in `_protocol.py` even claimed it was
"matching the MCP specification". It is not: the official SDK's
`serializeMessage` is `JSON.stringify(message) + '\\n'`, with no header
anywhere.

Because the client and the servers shared the same misreading they talked to
each other perfectly, which is why it survived so long. Nothing else could
reach either of them: the five domain servers were unusable from VS Code,
Claude Desktop or Cursor, and the client rejected every third-party server on
its first reply with "MCP response missing Content-Length header".

These tests are about the bytes on the wire, so they assert on the bytes.
"""
from __future__ import annotations

import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

from coding_agent.plugins.mcp import _read_lsp_message
from coding_agent.plugins.mcp_servers._protocol import McpStdioServer

REPO = str(Path(__file__).resolve().parents[1])
DOMAIN_SERVERS = ["office_mcp", "finance_mcp"]

# office_mcp builds a ToolRegistry, which needs a workspace root; finance_mcp
# takes none. Passing one to a server that ignores it is harmless.
def _server_argv(module: str, root: str) -> list[str]:
    return [sys.executable, "-m", f"coding_agent.plugins.mcp_servers.{module}", root]


def _newline_request(**kwargs) -> bytes:
    return json.dumps({"jsonrpc": "2.0", **kwargs}).encode() + b"\n"


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module", DOMAIN_SERVERS)
def test_server_answers_newline_delimited_json(module: str) -> None:
    """One JSON object per line, no headers -- what every MCP client reads."""
    stdin = (
        _newline_request(id=1, method="initialize", params={
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        + _newline_request(id=2, method="tools/list", params={})
    )
    proc = subprocess.run(
        _server_argv(module, REPO), input=stdin,
        capture_output=True, cwd=REPO, timeout=90,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    assert len(lines) == 2, f"expected one line per reply, got {proc.stdout[:200]!r}"
    assert b"Content-Length" not in proc.stdout, "still emitting LSP framing"

    handshake = json.loads(lines[0])
    assert handshake["result"]["serverInfo"]["name"].startswith("yucode-")
    listed = json.loads(lines[1])
    assert listed["result"]["tools"], f"{module} advertised no tools"


def test_server_still_accepts_the_old_header_framing() -> None:
    """A configuration written against the old convention must keep working."""
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                   "clientInfo": {"name": "t", "version": "1"}},
    }).encode()
    stdin = b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
    proc = subprocess.run(
        _server_argv("office_mcp", REPO),
        input=stdin, capture_output=True, cwd=REPO, timeout=90,
    )
    assert json.loads(proc.stdout.decode().splitlines()[0])["result"]["serverInfo"]


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

def test_client_reads_a_newline_delimited_reply() -> None:
    """The regression that locked yucode out of every third-party server."""
    payload = {"jsonrpc": "2.0", "id": 1, "result": {"tools": []}}
    stream = io.BytesIO(json.dumps(payload).encode() + b"\n")
    assert _read_lsp_message(stream) == payload


def test_client_still_reads_header_framing() -> None:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}).encode()
    stream = io.BytesIO(b"Content-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body)
    assert _read_lsp_message(stream)["result"] == {"ok": True}


def test_client_skips_blank_lines_between_messages() -> None:
    payload = {"jsonrpc": "2.0", "id": 7, "result": {}}
    stream = io.BytesIO(b"\n\n" + json.dumps(payload).encode() + b"\n")
    assert _read_lsp_message(stream)["id"] == 7


def test_client_reports_a_closed_stream_rather_than_hanging() -> None:
    with pytest.raises(RuntimeError, match="closed the stream"):
        _read_lsp_message(io.BytesIO(b""))


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------

def test_server_and_client_agree_on_the_format() -> None:
    """What the server writes is what the client reads, without a subprocess."""
    server = McpStdioServer("roundtrip")
    server.register_tool("ping", "ping", {"type": "object", "properties": {}},
                         lambda args: "pong")
    written: list[bytes] = []

    class _Out:
        buffer = type("B", (), {
            "write": staticmethod(lambda b: written.append(b)),
            "flush": staticmethod(lambda: None),
        })()

    original = sys.stdout
    sys.stdout = _Out()  # type: ignore[assignment]
    try:
        server._send({"jsonrpc": "2.0", "id": 1, "result": {"tools": []}})
    finally:
        sys.stdout = original

    assert _read_lsp_message(io.BytesIO(b"".join(written)))["id"] == 1


# ---------------------------------------------------------------------------
# The served directory is a boundary
# ---------------------------------------------------------------------------
# These servers took a path from the caller and read or wrote it unchecked, and
# four of their tools write: write_cell saves the workbook, write_new and
# append_text save the document, create_pptx and data_to_pptx mkdir their
# parents first. That was survivable only while the wire format was wrong and
# the caller could only ever be yucode itself. Making them reachable from
# Copilot made the paths come from a model this machine's owner does not
# control.

def _call(module: str, tool: str, arguments: dict, root: str) -> dict:
    import os
    stdin = (
        _newline_request(id=1, method="initialize", params={
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        + _newline_request(id=2, method="tools/call",
                           params={"name": tool, "arguments": arguments})
    )
    env = {**os.environ, "YUCODE_MCP_ROOT": root}
    proc = subprocess.run(
        _server_argv(module, root), input=stdin,
        capture_output=True, cwd=REPO, timeout=90, env=env,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    return json.loads(lines[1])["result"]


@pytest.mark.parametrize("module,tool,args", [
    ("office_mcp", "write_excel_cell",
     {"path": "/tmp/yucode_escape.xlsx", "sheet": "S", "cell": "A1", "value": "x"}),
    ("office_mcp", "write_word", {"path": "/tmp/yucode_escape.docx", "text": "x"}),
    ("office_mcp", "read_excel_sheet", {"path": "/etc/hosts"}),
    ("office_mcp", "read_pdf_text", {"path": "/etc/hosts"}),
])
def test_absolute_path_outside_the_root_is_refused(module, tool, args, tmp_path):
    result = _call(module, tool, args, str(tmp_path))
    assert result["isError"], f"{module}.{tool} accepted {args}"
    assert "outside the served directory" in result["content"][0]["text"]


def test_dot_dot_cannot_climb_out(tmp_path):
    result = _call("office_mcp", "read_excel_sheet",
                   {"path": "../../../../etc/hosts"}, str(tmp_path))
    assert result["isError"]


def test_a_refused_write_leaves_no_file(tmp_path):
    """The refusal has to come before the handler, not after it saves."""
    target = tmp_path.parent / "yucode_should_not_exist.docx"
    if target.exists():
        target.unlink()
    _call("office_mcp", "write_word", {"path": str(target), "text": "x"}, str(tmp_path))
    assert not target.exists()


def test_a_path_inside_the_root_still_works(tmp_path):
    """A relative path must resolve against the served root, not the process
    working directory -- the bug the path guard's rewrite exists to close."""
    (tmp_path / "note.docx").unlink(missing_ok=True)
    result = _call("office_mcp", "write_word",
                   {"path": "note.docx", "text": "hello"}, str(tmp_path))
    assert not result["isError"], result["content"][0]["text"]
    assert (tmp_path / "note.docx").exists(), "written somewhere other than the root"


# ---------------------------------------------------------------------------
# office_mcp: the tools that are actually worth exposing
# ---------------------------------------------------------------------------

def test_office_server_exposes_the_domain_tools(tmp_path):
    """The thin servers reimplement generic file I/O and basic statistics,
    which any assistant already has. These are the ones it cannot work out."""
    stdin = (
        _newline_request(id=1, method="initialize", params={
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        })
        + _newline_request(id=2, method="tools/list", params={})
    )
    proc = subprocess.run(
        [sys.executable, "-m", "coding_agent.plugins.mcp_servers.office_mcp",
         str(tmp_path)],
        input=stdin, capture_output=True, cwd=REPO, timeout=90,
    )
    lines = [ln for ln in proc.stdout.decode().splitlines() if ln.strip()]
    names = {t["name"] for t in json.loads(lines[1])["result"]["tools"]}
    for required in (
        "locate_databook_stage_columns", "write_pptx_from_template",
        "fill_pptx_table", "estimate_pptx_text_capacity",
        "inspect_excel_sheets", "extract_pdf_period_table",
    ):
        assert required in names, f"{required} is not reachable over MCP"


def test_office_server_shares_one_implementation_with_the_cli(tmp_path):
    """No second copy of the domain logic: the MCP surface is the same
    office_tools() the agent itself runs, so a fix reaches both."""
    from coding_agent.config.settings import load_app_config
    from coding_agent.plugins.mcp_servers.office_mcp import build_server
    from coding_agent.tools import ToolRegistry
    from coding_agent.tools.office import office_tools

    server = build_server(tmp_path)
    registry = ToolRegistry(tmp_path, load_app_config(None, workspace=tmp_path))
    assert set(server._tools) == {d.spec.name for d in office_tools(registry)}
