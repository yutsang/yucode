# Using yucode's domain tools from GitHub Copilot

The company is standardising on GitHub Copilot. That settles the general
coding-assistant question and leaves a narrower one worth answering: Copilot
has no idea what a databook stage column is, how this firm's FDD output is
laid out, or which PPTX shapes a deck template expects. That knowledge lives
in this repo, in about 3,000 lines, and it does not have to be thrown away
just because the chat window belongs to someone else now.

None of this requires a VS Code extension, a marketplace listing, TypeScript,
or an `.exe`. Copilot's agent mode speaks MCP, and yucode already ships five
MCP servers. Point one at the other.

## Setup

Put this in `.vscode/mcp.json` in the workspace (or the equivalent block in
user settings, if it should apply everywhere):

```json
{
  "servers": {
    "yucode-office": {
      "type": "stdio",
      "command": "python",
      "args": [
        "-m", "coding_agent.plugins.mcp_servers.office_mcp",
        "C:\\path\\to\\the\\engagement\\folder"
      ],
      "cwd": "C:\\path\\to\\yucode"
    }
  }
}
```

Two directories, doing different jobs. `cwd` is where yucode lives, so
`python -m` can find the module. The argument is the folder the tools are
allowed to touch — every path argument is resolved against it, and one that
lands outside is refused before the handler runs. Point it at the engagement
folder, not at the whole drive.

`python -m` deliberately: nothing is installed, nothing is compiled, no
executable is produced for endpoint protection to object to. The servers are
plain Python reading stdin and writing stdout.

Open Copilot Chat, switch to agent mode, and the tools appear in the tool
picker.

## What `office_mcp` exposes

It serves the 22 tools from `coding_agent/tools/office.py` — the same
implementations the agent itself runs, so there is one copy to maintain and a
fix reaches both. That set includes the things worth reaching for:

| | |
| --- | --- |
| Databook | `locate_databook_stage_columns`, `read_excel_preview`, `inspect_excel_sheets` (hidden rows, columns and sheets) |
| PPTX | `write_pptx_from_template`, `inspect_pptx_shapes`, `fill_pptx_shape_text`, `fill_pptx_table`, `estimate_pptx_text_capacity` |
| Excel | `read_excel_sheet`, `list_excel_sheets`, `write_excel_cell`, `excel_to_json` |
| Word | `read_word_text`, `read_word_paragraphs`, `write_word`, `append_word` |
| PDF | `read_pdf_text`, `extract_pdf_words`, `extract_pdf_period_table` |

`excel_mcp`, `docx_mcp`, `pdf_mcp` and `databook_mcp` used to sit alongside it
and have been removed. They were standalone reimplementations that predated
this bridge: `analyze_data` computed min/max/mean, `read_sheet` returned
headers and rows, `data_to_pptx` joined table rows with pipes and pushed the
text into a placeholder. Copilot does all of that unaided, so they were a
second copy of the domain logic that added no capability and could be
configured by mistake in place of the real one.

Two things they had that `office.py` did not were moved across first, into
`write_pptx`: speaker `notes`, and a `layout` index per slide so a deck can
open with a title slide instead of every slide using "Title and Content".

`finance_mcp` remains, having no `office.py` equivalent — `quote`, `history`,
`financials`, `news`, `compare`. It needs outbound network access, which a
locked-down machine will usually refuse.

## Adding it from the CLI

`yucode mcp-preset office` writes the server into the workspace MCP config, if
you would rather not hand-edit JSON. The old `excel`, `word` and `pdf` presets
are gone with the servers they pointed at; `office` replaces all three.

## A note on why this did not work before

Both halves of yucode's MCP support framed messages with `Content-Length`
headers. That is LSP's convention, not MCP's — the stdio transport is
newline-delimited JSON, and the official SDK's `serializeMessage` is
`JSON.stringify(message) + '\n'` with no header at all.

Client and servers shared the misreading, so they talked to each other
perfectly and nothing looked broken. Nothing else could reach either of them:
these five servers were invisible to VS Code, Claude Desktop and Cursor, and
yucode's own client rejected every third-party server on its first reply.

Fixed 2026-09-01, verified in both directions against the official SDK, and
pinned by `tests/test_mcp_wire_format.py`.
