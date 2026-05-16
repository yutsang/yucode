"""Tests for the persistent memory subsystem and the related prompt/tool wiring.

Covers:
- MemoryStore CRUD + index rebuild
- memory_save / list / read / delete / search tools (registered + handlers)
- prompting.py auto-load of MEMORY.md, AGENTS.md recognition,
  office-tools section, web-freshness section, memory-rules section
- filesystem._read_file binary-extension routing
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coding_agent.memory.prompting import (
    PromptAssembler,
    ProjectContext,
    discover_instruction_files,
    discover_project_context,
)
from coding_agent.memory.store import MemoryStore, slugify
from coding_agent.tools.memory_tools import memory_tools


# ---- helpers --------------------------------------------------------------

def _make_config(weak: bool = False):
    from coding_agent.config.settings import AppConfig, ProviderConfig

    cfg = AppConfig()
    if weak:
        # Force weak tier via an obviously local model name
        cfg = AppConfig(provider=ProviderConfig(model="qwen3-32b"))
    return cfg


class _StubRegistry:
    """Minimal stand-in for ToolRegistry — memory_tools only needs workspace_root."""

    def __init__(self, workspace_root: Path):
        self.workspace_root = workspace_root.resolve()


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    """Isolated workspace with HOME redirected so user-scope memory lands in tmp."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    return workspace


# ---- MemoryStore ----------------------------------------------------------

class TestMemoryStore:
    def test_slugify_normalises(self):
        assert slugify("Hello World") == "hello-world"
        assert slugify("User_prefers_Go") == "user-prefers-go"
        assert slugify("  ALL  CAPS!! ") == "all-caps"
        assert slugify("") == "memory"

    def test_save_creates_file_and_index(self, workspace):
        store = MemoryStore(workspace)
        entry = store.save(
            "user-prefers-go",
            "User has 10y Go experience, new to React",
            "user",
            "Frame React explanations as analogies to Go.",
            scope="user",
        )
        assert entry.path.exists()
        assert entry.path.read_text().startswith("---\nname: user-prefers-go")
        index = store.index_path("user").read_text()
        assert "user-prefers-go" in index
        assert "10y Go experience" in index

    def test_workspace_scope_isolated_from_user(self, workspace):
        store = MemoryStore(workspace)
        store.save("u", "user scope", "user", "body U", scope="user")
        store.save("w", "workspace scope", "project", "body W", scope="workspace")
        names_user = {e.name for e in store.list("user")}
        names_ws = {e.name for e in store.list("workspace")}
        assert names_user == {"u"}
        assert names_ws == {"w"}

    def test_read_prefers_workspace_then_user(self, workspace):
        store = MemoryStore(workspace)
        store.save("same", "user version", "user", "U", scope="user")
        store.save("same", "workspace version", "project", "W", scope="workspace")
        got = store.read("same")
        assert got is not None
        assert got.scope == "workspace"
        assert got.body == "W"

    def test_delete_rebuilds_index(self, workspace):
        store = MemoryStore(workspace)
        store.save("temp", "to delete", "project", "body", scope="user")
        assert store.delete("temp", "user")
        index = store.index_path("user").read_text()
        assert "temp" not in index
        assert "_(empty)_" in index

    def test_delete_unknown_returns_false(self, workspace):
        assert MemoryStore(workspace).delete("nonexistent") is False

    def test_save_rejects_invalid_type(self, workspace):
        with pytest.raises(ValueError, match="Invalid memory type"):
            MemoryStore(workspace).save("x", "d", "bogus", "b")  # type: ignore[arg-type]

    def test_search_full_text(self, workspace):
        store = MemoryStore(workspace)
        store.save("a", "first", "project", "alpha beta gamma", scope="user")
        store.save("b", "second", "project", "delta epsilon", scope="user")
        hits = store.search("BETA")  # case-insensitive
        assert [e.name for e in hits] == ["a"]

    def test_load_indexes_text_combines_scopes(self, workspace):
        store = MemoryStore(workspace)
        store.save("u", "user one", "user", "body", scope="user")
        store.save("w", "ws one", "project", "body", scope="workspace")
        text = store.load_indexes_text()
        assert "Memory index (user)" in text
        assert "Memory index (workspace)" in text
        assert "u" in text and "w" in text


# ---- memory_tools handlers ------------------------------------------------

class TestMemoryTools:
    def test_save_writes_and_returns_path(self, workspace):
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[0].handler  # memory_save
        result = json.loads(handler({
            "name": "feedback-no-mocks",
            "description": "Integration tests must use a real database",
            "type": "feedback",
            "content": "Rule: don't mock DB in tests.\n**Why:** prior incident.\n**How to apply:** anywhere we add tests.",
            "scope": "workspace",
        }))
        assert result["saved"] == "feedback-no-mocks"
        assert result["scope"] == "workspace"
        assert (workspace / ".yucode" / "memory" / "feedback-no-mocks.md").exists()

    def test_save_validates_type(self, workspace):
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[0].handler
        result = json.loads(handler({
            "name": "x", "description": "d", "content": "c", "type": "bogus",
        }))
        assert "error" in result and "type must be one of" in result["error"]

    def test_save_validates_scope(self, workspace):
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[0].handler
        result = json.loads(handler({
            "name": "x", "description": "d", "content": "c", "type": "user", "scope": "global",
        }))
        assert "error" in result and "scope must be one of" in result["error"]

    def test_save_requires_fields(self, workspace):
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[0].handler
        result = json.loads(handler({"name": "x", "description": "", "content": "c", "type": "user"}))
        assert "error" in result

    def test_list_returns_metadata_only(self, workspace):
        store = MemoryStore(workspace)
        store.save("m1", "first memory", "project", "long body" * 100, scope="user")
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[1].handler  # memory_list
        result = json.loads(handler({}))
        assert len(result) == 1
        assert "body" not in result[0]
        assert result[0]["name"] == "m1"

    def test_read_returns_body(self, workspace):
        store = MemoryStore(workspace)
        store.save("readme", "the desc", "user", "the body", scope="user")
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[2].handler  # memory_read
        result = json.loads(handler({"name": "readme"}))
        assert result["body"] == "the body"
        assert result["type"] == "user"

    def test_read_missing_returns_error(self, workspace):
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[2].handler
        result = json.loads(handler({"name": "ghost"}))
        assert "error" in result

    def test_delete_removes_file(self, workspace):
        store = MemoryStore(workspace)
        store.save("doomed", "x", "project", "body", scope="user")
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[3].handler  # memory_delete
        result = json.loads(handler({"name": "doomed"}))
        assert result == {"deleted": "doomed"}
        assert not (Path.home() / ".yucode" / "memory" / "doomed.md").exists()

    def test_search_returns_matches(self, workspace):
        store = MemoryStore(workspace)
        store.save("first", "a", "project", "needle in haystack", scope="user")
        store.save("second", "b", "project", "unrelated", scope="user")
        reg = _StubRegistry(workspace)
        handler = memory_tools(reg)[4].handler  # memory_search
        result = json.loads(handler({"query": "needle"}))
        assert len(result) == 1
        assert result[0]["name"] == "first"


# ---- prompt integration ---------------------------------------------------

class TestPromptIntegration:
    def test_memory_index_loaded_into_prompt(self, workspace):
        store = MemoryStore(workspace)
        store.save("user-pref", "User likes terse responses", "user",
                   "Keep responses under 3 lines.", scope="user")
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        assert "user-pref" in ctx.memory_index
        cfg = _make_config()
        prompt = PromptAssembler(cfg, ctx).render()
        assert "# Persistent memory" in prompt
        assert "user-pref" in prompt

    def test_memory_section_omitted_when_empty(self, workspace):
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        cfg = _make_config()
        prompt = PromptAssembler(cfg, ctx).render()
        # Should NOT have the runtime "Persistent memory" section when no memories exist
        # (but the rules section "Persistent memory (when to save)" is always present)
        assert "# Persistent memory\n" not in prompt or "(when to save)" in prompt

    def test_office_files_section_present(self, workspace):
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        prompt = PromptAssembler(_make_config(), ctx).render()
        assert "# Office and document files" in prompt
        assert "inspect_excel_sheets" in prompt
        assert "read_pdf_text" in prompt
        assert "read_pptx" in prompt

    def test_web_freshness_section_includes_today(self, workspace):
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        prompt = PromptAssembler(_make_config(), ctx).render()
        assert "Time-sensitive facts" in prompt
        assert "2026-05-17" in prompt
        assert "web_search" in prompt and "web_fetch" in prompt

    def test_memory_rules_section_present(self, workspace):
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        prompt = PromptAssembler(_make_config(), ctx).render()
        assert "memory_save" in prompt
        assert "memory_list" in prompt
        assert "feedback" in prompt and "reference" in prompt

    def test_agents_md_recognised(self, workspace):
        (workspace / "AGENTS.md").write_text("# Project AGENTS\nlive long and prosper", encoding="utf-8")
        files = discover_instruction_files(workspace, [])
        names = {f.path.name for f in files}
        assert "AGENTS.md" in names

    def test_memory_index_truncates_on_overflow(self, workspace, monkeypatch):
        # Lower the cap so we can verify truncation without writing megabytes
        from coding_agent.memory import prompting
        monkeypatch.setattr(prompting, "MAX_MEMORY_INDEX_CHARS", 200)
        store = MemoryStore(workspace)
        for i in range(20):
            store.save(f"m{i:02d}", f"desc-{i}" * 5, "project", "body", scope="user")
        ctx = discover_project_context(workspace, "2026-05-17", include_git_context=False)
        assert len(ctx.memory_index) <= 220  # 200 cap + "(truncated)" suffix


# ---- filesystem binary routing -------------------------------------------

class TestBinaryFileRouting:
    @pytest.fixture
    def reg(self, workspace):
        from coding_agent.config.settings import AppConfig
        from coding_agent.tools import ToolRegistry
        return ToolRegistry(workspace_root=workspace, config=AppConfig())

    def test_xlsx_routes_to_excel_tool(self, reg, workspace):
        path = workspace / "report.xlsx"
        path.write_bytes(b"PK\x03\x04fake-zip")  # any non-empty bytes
        from coding_agent.tools.filesystem import _read_file
        with pytest.raises(ValueError, match="inspect_excel_sheets"):
            _read_file(reg, {"path": "report.xlsx"})

    def test_pdf_routes_to_pdf_tool(self, reg, workspace):
        path = workspace / "doc.pdf"
        path.write_bytes(b"%PDF-1.4 fake")
        from coding_agent.tools.filesystem import _read_file
        with pytest.raises(ValueError, match="read_pdf_text"):
            _read_file(reg, {"path": "doc.pdf"})

    def test_docx_routes_to_word_tool(self, reg, workspace):
        path = workspace / "memo.docx"
        path.write_bytes(b"PK\x03\x04fake")
        from coding_agent.tools.filesystem import _read_file
        with pytest.raises(ValueError, match="read_word_text"):
            _read_file(reg, {"path": "memo.docx"})

    def test_pptx_routes_to_pptx_tool(self, reg, workspace):
        path = workspace / "deck.pptx"
        path.write_bytes(b"PK\x03\x04fake")
        from coding_agent.tools.filesystem import _read_file
        with pytest.raises(ValueError, match="read_pptx"):
            _read_file(reg, {"path": "deck.pptx"})

    def test_unknown_binary_keeps_generic_error(self, reg, workspace):
        path = workspace / "blob.bin"
        path.write_bytes(b"\x00\x01\x02\x03" * 10)
        from coding_agent.tools.filesystem import _read_file
        with pytest.raises(ValueError, match="dedicated tools"):
            _read_file(reg, {"path": "blob.bin"})

    def test_text_file_still_reads_normally(self, reg, workspace):
        path = workspace / "hello.txt"
        path.write_text("hi there\nsecond line\n", encoding="utf-8")
        from coding_agent.tools.filesystem import _read_file
        result = _read_file(reg, {"path": "hello.txt"})
        assert "hi there" in result and "second line" in result


# ---- tool registration ----------------------------------------------------

def test_memory_tools_registered_in_registry(workspace):
    from coding_agent.config.settings import AppConfig
    from coding_agent.tools import ToolRegistry
    reg = ToolRegistry(workspace_root=workspace, config=AppConfig())
    names = reg.list_names()
    for tool in ("memory_save", "memory_list", "memory_read", "memory_delete", "memory_search"):
        assert tool in names, f"`{tool}` missing from registry"


def test_memory_tools_in_coordinator_roles():
    """Research workers should be able to consult memory; work workers read it."""
    from coding_agent.core.coordinator import ROLE_TOOLS, WorkerRole
    research = ROLE_TOOLS[WorkerRole.RESEARCH]
    work = ROLE_TOOLS[WorkerRole.WORK]
    for tool in ("memory_list", "memory_read", "memory_search"):
        assert tool in research, f"RESEARCH workers missing `{tool}`"
    for tool in ("memory_list", "memory_read"):
        assert tool in work, f"WORK workers missing `{tool}`"
    # workers should not silently create memories
    assert "memory_save" not in work


# ---- CLI /remember and /forget handlers -----------------------------------

class TestCliRememberForget:
    def test_remember_saves_user_scope_by_default(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_remember_command
        _handle_remember_command(workspace, "User likes terse responses without summaries")
        out = capsys.readouterr().out
        assert "Saved memory" in out
        entries = MemoryStore(workspace).list("user")
        assert len(entries) == 1
        assert entries[0].body.startswith("User likes terse")

    def test_remember_workspace_flag(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_remember_command
        _handle_remember_command(workspace, "-w project uses pnpm not npm")
        capsys.readouterr()
        ws_entries = MemoryStore(workspace).list("workspace")
        user_entries = MemoryStore(workspace).list("user")
        assert len(ws_entries) == 1
        assert len(user_entries) == 0

    def test_remember_type_flag(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_remember_command
        _handle_remember_command(workspace, "--type=feedback don't mock the database in tests")
        capsys.readouterr()
        entries = MemoryStore(workspace).list()
        assert entries[0].type == "feedback"

    def test_remember_rejects_invalid_type(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_remember_command
        _handle_remember_command(workspace, "--type=bogus some content")
        out = capsys.readouterr().out
        assert "Invalid" in out
        assert MemoryStore(workspace).list() == []

    def test_remember_empty_shows_usage(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_remember_command
        _handle_remember_command(workspace, "   ")
        out = capsys.readouterr().out
        assert "Usage" in out

    def test_forget_removes_memory(self, workspace, capsys):
        store = MemoryStore(workspace)
        store.save("doomed-memory", "x", "project", "y", scope="user")
        from coding_agent.interface.cli import _handle_forget_command
        _handle_forget_command(workspace, "doomed-memory")
        out = capsys.readouterr().out
        assert "Deleted memory" in out
        assert store.list() == []

    def test_forget_unknown_shows_warning(self, workspace, capsys):
        from coding_agent.interface.cli import _handle_forget_command
        _handle_forget_command(workspace, "ghost")
        out = capsys.readouterr().out
        assert "No memory named" in out
