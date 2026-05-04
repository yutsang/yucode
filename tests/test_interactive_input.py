"""Tests for the prompt_toolkit-based interactive input session."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from prompt_toolkit.document import Document

from coding_agent.interface.cli import _SLASH_COMMANDS, _AT_HIDDEN, _make_pt_session


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
