"""Tests for the v0.6.0 features: time-sensitive runtime detector, memory
staleness markers, image_read tool, /init AGENTS.md generator, user-profile
memory bootstrap, web_search fallback chain."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

from coding_agent.config.settings import AppConfig
from coding_agent.core.runtime import (
    _ToolObservations,
    _check_final_answer_grounding,
    _is_time_sensitive_prompt,
)
from coding_agent.interface.init_workspace import (
    detect_profile,
    render_agents_md,
    write_agents_md,
)
from coding_agent.interface.memory_bootstrap import (
    USER_PROFILE_NAME,
    bootstrap_user_profile,
    gather_facts,
    render_user_profile_body,
)
from coding_agent.memory.store import (
    STALE_DAYS_THRESHOLD,
    MemoryEntry,
    MemoryStore,
)
from coding_agent.tools import ToolRegistry
from coding_agent.tools.office import _image_read
from coding_agent.tools.web import _duckduckgo_search, _relax_query, _web_search


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


# ---- Time-sensitive runtime detector ---------------------------------------

class TestTimeSensitiveDetector:
    @pytest.mark.parametrize("prompt", [
        "what's the latest version of React?",
        "current CEO of Apple",
        "今天的天氣怎麼樣",
        "最新的 iOS 版本",
        "現在的匯率是多少",
        "今年 World Cup 結果",
        "tell me the most recent earthquake",
        "what is the current exchange rate USD CNY",
    ])
    def test_detects_time_sensitive_prompts(self, prompt):
        assert _is_time_sensitive_prompt(prompt), f"should flag: {prompt}"

    @pytest.mark.parametrize("prompt", [
        "how does the runtime call providers?",
        "find the function that parses YAML",
        "refactor this module",
        "where is the validator defined",
        "",
    ])
    def test_skips_static_prompts(self, prompt):
        assert not _is_time_sensitive_prompt(prompt), f"should not flag: {prompt}"

    def test_grounding_fires_when_no_web_search(self):
        obs = _ToolObservations()
        violation = _check_final_answer_grounding(
            obs, "The answer is Dragonair.",
            is_weak_investigation=False,
            is_time_sensitive=True,
        )
        assert violation is not None
        assert violation.reason == "time_sensitive_no_web_search"

    def test_grounding_skips_when_web_search_called(self):
        obs = _ToolObservations()
        obs.web_searched = True
        violation = _check_final_answer_grounding(
            obs, "The answer is Cathay Pacific.",
            is_weak_investigation=False,
            is_time_sensitive=True,
        )
        assert violation is None

    def test_grounding_skips_when_web_fetch_called(self):
        obs = _ToolObservations()
        obs.web_fetched = True
        violation = _check_final_answer_grounding(
            obs, "any answer",
            is_weak_investigation=False,
            is_time_sensitive=True,
        )
        assert violation is None

    def test_grounding_skips_for_non_time_sensitive(self):
        obs = _ToolObservations()
        violation = _check_final_answer_grounding(
            obs, "the function is at line 42",
            is_weak_investigation=False,
            is_time_sensitive=False,
        )
        assert violation is None


# ---- Memory staleness markers ----------------------------------------------

class TestMemoryStaleness:
    def test_saved_at_recorded(self, workspace):
        entry = MemoryStore(workspace).save("recent", "x", "user", "body")
        assert entry.saved_at == date.today().isoformat()
        # File should contain it too
        text = entry.path.read_text()
        assert "saved_at:" in text and date.today().isoformat() in text

    def test_age_days_zero_for_today(self, workspace):
        e = MemoryStore(workspace).save("today", "x", "user", "body")
        assert e.age_days() == 0

    def test_age_days_none_for_legacy(self):
        e = MemoryEntry(
            name="legacy", description="d", type="user", body="b",
            path=Path("/tmp/legacy.md"), scope="user", saved_at="",
        )
        assert e.age_days() is None

    def test_age_days_handles_bad_iso(self):
        e = MemoryEntry(
            name="legacy", description="d", type="user", body="b",
            path=Path("/tmp/legacy.md"), scope="user", saved_at="not-a-date",
        )
        assert e.age_days() is None

    def test_load_indexes_text_marks_stale(self, workspace):
        store = MemoryStore(workspace)
        store.save("fresh", "fresh entry", "user", "body")
        # Forge a stale entry by writing the frontmatter directly
        stale_path = store.root_for("user") / "stale.md"
        stale_date = "2020-01-01"
        stale_path.write_text(
            "---\nname: stale\ndescription: ancient entry\n"
            f"saved_at: {stale_date}\nmetadata:\n  type: project\n---\n\nbody\n"
        )
        text = store.load_indexes_text()
        assert "stale" in text
        assert "[stale: " in text
        # Fresh entry should NOT carry a stale marker
        fresh_line = next(line for line in text.splitlines() if line.startswith("- `fresh`"))
        assert "[stale" not in fresh_line

    def test_stale_threshold_constant_exposed(self):
        assert STALE_DAYS_THRESHOLD >= 1


# ---- image_read tool -------------------------------------------------------

class TestImageRead:
    @pytest.fixture
    def reg(self, workspace):
        return ToolRegistry(workspace_root=workspace, config=AppConfig())

    def test_image_read_basic_metadata(self, reg, workspace):
        path = workspace / "test.png"
        # Minimal 1x1 PNG; openable by PIL but valid raw bytes
        path.write_bytes(
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00"
            b"\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx"
            b"\x9cc\xf8\xcf\xc0\x00\x00\x00\x03\x00\x01\xc1\xc4N\xe0\x00"
            b"\x00\x00\x00IEND\xaeB`\x82"
        )
        result = json.loads(_image_read(reg, {"path": "test.png"}))
        assert result["mime_type"] == "image/png"
        assert result["size_bytes"] > 0
        # PIL may or may not be available; either width or a degradation note
        assert "width" in result or "dimensions_note" in result or "dimensions_error" in result

    def test_image_read_rejects_unknown_extension(self, reg, workspace):
        path = workspace / "weird.xyz"
        path.write_bytes(b"\x00")
        with pytest.raises(ValueError, match="Unsupported image extension"):
            _image_read(reg, {"path": "weird.xyz"})

    def test_image_read_missing_file(self, reg, workspace):
        with pytest.raises(FileNotFoundError):
            _image_read(reg, {"path": "ghost.png"})

    def test_image_read_with_base64(self, reg, workspace):
        path = workspace / "tiny.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        result = json.loads(_image_read(reg, {"path": "tiny.png", "include_base64": True}))
        assert "data_url" in result
        assert result["data_url"].startswith("data:image/png;base64,")

    def test_image_read_skips_base64_when_too_large(self, reg, workspace):
        path = workspace / "big.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (2 * 1024 * 1024))
        result = json.loads(_image_read(reg, {"path": "big.png", "include_base64": True}))
        assert "base64_skipped" in result
        assert "data_url" not in result


def test_image_read_registered(workspace):
    reg = ToolRegistry(workspace_root=workspace, config=AppConfig())
    assert "image_read" in reg.list_names()


def test_image_extensions_route_to_image_read(workspace):
    reg = ToolRegistry(workspace_root=workspace, config=AppConfig())
    from coding_agent.tools.filesystem import _read_file
    for ext in (".png", ".jpg", ".webp"):
        path = workspace / f"img{ext}"
        path.write_bytes(b"\x00" * 16)
        with pytest.raises(ValueError, match="image_read"):
            _read_file(reg, {"path": f"img{ext}"})


# ---- /init AGENTS.md generator --------------------------------------------

class TestInitWorkspace:
    def test_detect_python_project(self, workspace):
        (workspace / "pyproject.toml").write_text(
            "[tool.poetry]\nname = \"foo\"\nversion = \"0.1.0\"\n"
            "[tool.poetry.dependencies]\nfastapi = \"*\"\n",
            encoding="utf-8",
        )
        (workspace / "tests").mkdir()
        profile = detect_profile(workspace)
        assert "Python" in profile.languages
        assert profile.package_manager == "poetry"
        assert "FastAPI" in profile.frameworks
        assert profile.test_command and "pytest" in profile.test_command

    def test_detect_node_project(self, workspace):
        (workspace / "package.json").write_text(json.dumps({
            "name": "frontend",
            "scripts": {"test": "jest", "build": "vite build", "dev": "vite"},
            "dependencies": {"react": "^18", "next": "^14"},
        }), encoding="utf-8")
        (workspace / "tsconfig.json").write_text("{}", encoding="utf-8")
        (workspace / "pnpm-lock.yaml").write_text("", encoding="utf-8")
        profile = detect_profile(workspace)
        assert "TypeScript" in profile.languages
        assert profile.package_manager == "pnpm"
        assert "React" in profile.frameworks
        assert "Next.js" in profile.frameworks
        assert profile.test_command and "jest" in profile.test_command

    def test_detect_rust_project(self, workspace):
        (workspace / "Cargo.toml").write_text("[package]\nname = \"x\"\n", encoding="utf-8")
        profile = detect_profile(workspace)
        assert "Rust" in profile.languages
        assert profile.test_command == "cargo test"

    def test_detect_go_project(self, workspace):
        (workspace / "go.mod").write_text("module foo\n", encoding="utf-8")
        profile = detect_profile(workspace)
        assert "Go" in profile.languages
        assert profile.test_command == "go test ./..."

    def test_render_agents_md_includes_sections(self, workspace):
        (workspace / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
        profile = detect_profile(workspace)
        md = render_agents_md(profile, workspace)
        assert "# " in md  # heading
        assert "## Stack" in md
        assert "## Notable files" in md
        assert "## Conventions" in md

    def test_write_agents_md_creates_file(self, workspace):
        (workspace / "go.mod").write_text("module x\n", encoding="utf-8")
        path, _ = write_agents_md(workspace)
        assert path.exists()
        assert path.name == "AGENTS.md"
        assert "Go" in path.read_text()

    def test_write_agents_md_refuses_overwrite_without_force(self, workspace):
        (workspace / "AGENTS.md").write_text("existing", encoding="utf-8")
        with pytest.raises(FileExistsError):
            write_agents_md(workspace)

    def test_write_agents_md_force_overwrites(self, workspace):
        (workspace / "AGENTS.md").write_text("existing", encoding="utf-8")
        path, _ = write_agents_md(workspace, force=True)
        assert "existing" not in path.read_text()


# ---- user-profile memory bootstrap ----------------------------------------

class TestMemoryBootstrap:
    def test_gather_facts_includes_os(self):
        facts = gather_facts()
        assert "os" in facts and facts["os"]

    def test_render_body_is_non_empty(self):
        facts = {"git_name": "X", "git_email": "x@y", "os": "Linux 6"}
        body = render_user_profile_body(facts)
        assert "X" in body and "x@y" in body and "Linux" in body

    def test_bootstrap_creates_user_memory(self, workspace):
        entry, was_new = bootstrap_user_profile(workspace)
        assert was_new
        assert entry.name == USER_PROFILE_NAME
        assert entry.scope == "user"
        # Re-running without force raises
        with pytest.raises(FileExistsError):
            bootstrap_user_profile(workspace)

    def test_bootstrap_force_refreshes(self, workspace):
        bootstrap_user_profile(workspace)
        entry, was_new = bootstrap_user_profile(workspace, force=True)
        assert not was_new
        assert entry.name == USER_PROFILE_NAME


# ---- web_search fallback chain --------------------------------------------

class TestWebSearchFallback:
    def test_relax_query_drops_quotes_and_filler(self):
        assert _relax_query('"latest" iPhone with USB-C') == "latest iPhone USB-C"
        assert _relax_query("what is the current CEO of Apple") == "current CEO Apple"

    def test_relax_query_preserves_short_when_all_dropped(self):
        # "a of in" — all filler / too short; should return original tokens
        result = _relax_query("a of in")
        assert result.strip()  # non-empty fallback

    def test_web_search_returns_meta_shape(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        with patch("coding_agent.tools.web._duckduckgo_search", return_value=[
            {"title": "Hello", "url": "https://example.com"}
        ]):
            result = json.loads(_web_search({"query": "hello"}))
        assert "results" in result
        assert "_meta" in result
        assert "duckduckgo" in result["_meta"]["backends_tried"]
        assert result["results"][0]["url"] == "https://example.com"

    def test_web_search_zero_hit_retries_with_relaxed(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        calls: list[str] = []

        def fake_ddg(q):
            calls.append(q)
            return [{"title": "Found", "url": "https://x.com"}] if calls[-1] != "the latest news" else []

        with patch("coding_agent.tools.web._duckduckgo_search", side_effect=fake_ddg):
            result = json.loads(_web_search({"query": "the latest news"}))
        assert "duckduckgo" in result["_meta"]["backends_tried"]
        assert "duckduckgo_relaxed" in result["_meta"]["backends_tried"]
        assert result["results"][0]["url"] == "https://x.com"

    def test_web_search_uses_brave_when_key_set(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "fake-key")
        with patch("coding_agent.tools.web._brave_search", return_value=[
            {"title": "Brave hit", "url": "https://brave.example.com"}
        ]) as brave_mock, patch("coding_agent.tools.web._duckduckgo_search") as ddg_mock:
            result = json.loads(_web_search({"query": "hello"}))
        assert brave_mock.called
        assert not ddg_mock.called
        assert "brave" in result["_meta"]["backends_tried"]
        assert result["results"][0]["url"] == "https://brave.example.com"

    def test_web_search_brave_failure_falls_back_to_ddg(self, monkeypatch):
        monkeypatch.setenv("BRAVE_API_KEY", "fake-key")
        with patch("coding_agent.tools.web._brave_search", side_effect=RuntimeError("boom")), \
             patch("coding_agent.tools.web._duckduckgo_search", return_value=[
                 {"title": "DDG hit", "url": "https://ddg.example.com"}
             ]):
            result = json.loads(_web_search({"query": "hello"}))
        assert "duckduckgo" in result["_meta"]["backends_tried"]
        assert result["results"][0]["url"] == "https://ddg.example.com"

    def test_web_search_empty_returns_hint(self, monkeypatch):
        monkeypatch.delenv("BRAVE_API_KEY", raising=False)
        with patch("coding_agent.tools.web._duckduckgo_search", return_value=[]):
            result = json.loads(_web_search({"query": "asdfqwerzxcv"}))
        assert result["results"] == []
        assert "hint" in result["_meta"]
