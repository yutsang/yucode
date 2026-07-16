"""Tests for the prompt_toolkit-based interactive input session."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from coding_agent.interface.cli import _SLASH_COMMANDS, _AT_HIDDEN, _make_pt_session, _read_prompt_line


@pytest.fixture()
def tmp_workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("")
    (tmp_path / "README.md").write_text("")
    (tmp_path / ".git").mkdir()
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / ".env").write_text("")
    return tmp_path


@pytest.fixture()
def completer(tmp_workspace: Path):
    session = _make_pt_session(tmp_workspace)
    return session.completer


def _complete(completer, text: str) -> list[str]:
    """Return plain display strings for all completions given the input text."""
    doc = Document(text, len(text))
    out = []
    for r in completer.get_completions(doc, None):
        # display is always FormattedText; extract the plain text fragments
        out.append("".join(fragment for _, fragment in r.display))
    return out


# ---------------------------------------------------------------------------
# Slash commands
# ---------------------------------------------------------------------------

class TestSlashCompletion:
    def test_prefix_matches(self, completer):
        results = _complete(completer, "/h")
        assert any("help" in r for r in results)

    def test_full_command_still_matches(self, completer):
        results = _complete(completer, "/help")
        assert results  # /help itself should still appear

    def test_no_match_returns_empty(self, completer):
        results = _complete(completer, "/zzz")
        assert results == []

    def test_slash_on_second_line_not_completed(self, completer):
        # A / on the second line of a multiline input is content, not a command
        results = _complete(completer, "first line\n/h")
        assert results == []

    def test_all_slash_commands_reachable(self, completer):
        # Every registered slash command should surface when typing just "/"
        results = _complete(completer, "/")
        displayed = [str(r) for r in results]
        # At least the most common commands must be present
        for cmd in ("/help", "/clear", "/exit"):
            assert any(cmd.lstrip("/") in d or cmd in d for d in displayed), f"{cmd} missing"


# ---------------------------------------------------------------------------
# @-file completion
# ---------------------------------------------------------------------------

class TestAtCompletion:
    def test_root_lists_visible_files(self, completer):
        results = _complete(completer, "@")
        # display strings are "@README.md", "@src/" etc.
        assert "@README.md" in results
        assert "@src/" in results

    def test_hidden_files_suppressed(self, completer):
        results = _complete(completer, "@")
        assert ".git/" not in results and ".git" not in results

    def test_pycache_suppressed(self, completer):
        results = _complete(completer, "@")
        assert "__pycache__/" not in results and "__pycache__" not in results

    def test_dot_prefix_reveals_hidden(self, completer):
        # When the user explicitly types @. they want hidden files (.env etc.)
        results = _complete(completer, "@.")
        # display strings look like "@.env", "@.github/" etc.
        assert any(r.startswith("@.") for r in results)

    def test_subdirectory_completion(self, completer):
        results = _complete(completer, "@src/")
        assert any("main.py" in r for r in results)

    def test_partial_match_in_subdir(self, completer):
        results = _complete(completer, "@src/m")
        assert any("main.py" in r for r in results), f"got: {results}"

    def test_no_at_token_returns_empty(self, completer):
        results = _complete(completer, "no at sign here")
        assert results == []

    def test_at_mid_sentence(self, completer):
        # The last @-token should be completed even mid-sentence
        results = _complete(completer, "look at @RE")
        assert any("README" in r for r in results), f"got: {results}"


# ---------------------------------------------------------------------------
# Session configuration
# ---------------------------------------------------------------------------

class TestSessionConfig:
    def test_multiline_enabled(self, tmp_workspace):
        session = _make_pt_session(tmp_workspace)
        assert session.multiline is True

    def test_history_configured(self, tmp_workspace):
        from prompt_toolkit.history import FileHistory
        session = _make_pt_session(tmp_workspace)
        assert isinstance(session.history, FileHistory)

    def test_completer_attached(self, tmp_workspace):
        session = _make_pt_session(tmp_workspace)
        assert session.completer is not None


# ---------------------------------------------------------------------------
# AT_HIDDEN constant
# ---------------------------------------------------------------------------

def test_at_hidden_contains_noise_dirs():
    assert ".git" in _AT_HIDDEN
    assert "__pycache__" in _AT_HIDDEN
    assert ".DS_Store" in _AT_HIDDEN


# ---------------------------------------------------------------------------
# WI-5: fallback when prompt_toolkit isn't installed (locked-down company PC)
# ---------------------------------------------------------------------------

class TestPromptToolkitFallback:
    def test_make_pt_session_returns_none_when_import_fails(self, tmp_workspace, monkeypatch):
        import builtins
        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "prompt_toolkit" or name.startswith("prompt_toolkit."):
                raise ImportError(f"No module named {name!r}")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        assert _make_pt_session(tmp_workspace) is None

    def test_read_prompt_line_uses_plain_input_when_session_is_none(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "  hello world  ")
        assert _read_prompt_line(None) == "hello world"

    def test_read_prompt_line_strips_whitespace_from_input(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "\tls -la\n")
        assert _read_prompt_line(None) == "ls -la"

    def test_read_prompt_line_propagates_eof_from_plain_input(self, monkeypatch):
        def raise_eof(prompt):
            raise EOFError()
        monkeypatch.setattr("builtins.input", raise_eof)
        with pytest.raises(EOFError):
            _read_prompt_line(None)

    def test_read_prompt_line_propagates_keyboard_interrupt_from_plain_input(self, monkeypatch):
        def raise_kbi(prompt):
            raise KeyboardInterrupt()
        monkeypatch.setattr("builtins.input", raise_kbi)
        with pytest.raises(KeyboardInterrupt):
            _read_prompt_line(None)

    def test_read_prompt_line_uses_pt_session_when_available(self):
        class _FakeSession:
            def __init__(self):
                self.calls = []

            def prompt(self, formatted_text):
                self.calls.append(formatted_text)
                return "  from prompt_toolkit  "

        fake = _FakeSession()
        assert _read_prompt_line(fake) == "from prompt_toolkit"
        assert len(fake.calls) == 1
