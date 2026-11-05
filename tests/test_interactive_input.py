"""Tests for the prompt_toolkit-based interactive input session."""
from __future__ import annotations

from collections import deque
from pathlib import Path

import pytest
from prompt_toolkit.buffer import Buffer, CompletionState
from prompt_toolkit.document import Document
from prompt_toolkit.keys import Keys

from coding_agent.interface.cli import _AT_HIDDEN, _make_pt_session, _read_prompt_line


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


# ---------------------------------------------------------------------------
# Enter key binding: distinguishing a real submit from a paste's embedded
# newlines leaking through as individual "enter" keystrokes.
#
# Windows has no native bracketed-paste signal (unlike POSIX terminals'
# \x1b[200~/\x1b[201~ markers): prompt_toolkit's Win32 console reader
# heuristically re-synthesizes a paste event from a batch of raw console
# input records only when that SAME batch contains >=1 newline AND >=1
# other character (see prompt_toolkit.input.win32.ConsoleInputReader
# ._is_paste). If the OS/terminal delivers a paste split across multiple
# such batches, a sub-batch containing an isolated newline fails that check
# and arrives as a bare "enter" KeyPress instead -- indistinguishable, at
# that point, from the user actually pressing Enter.
#
# The fix checks KeyPressEvent.key_processor.input_queue instead: a real,
# standalone Enter has nothing else queued up behind it, while a leaked
# paste-newline arrives back-to-back with the rest of the same burst
# (fed into the queue before being processed one key at a time). This is
# protocol-independent -- it doesn't matter whether the terminal supports
# bracketed paste at all.
# ---------------------------------------------------------------------------

class _FakeKeyProcessor:
    def __init__(self, input_queue):
        self.input_queue = input_queue


class _FakeEnterEvent:
    def __init__(self, buffer, input_queue):
        self.current_buffer = buffer
        self.key_processor = _FakeKeyProcessor(input_queue)


def _find_enter_handler(session):
    for binding in session.key_bindings.bindings:
        if tuple(binding.keys) == (Keys.ControlM,):
            return binding.handler
    raise AssertionError("no plain 'enter' binding found on the session")


class TestEnterKeyPasteHandling:
    def test_leaked_newline_with_more_keys_queued_inserts_newline_not_submit(self, tmp_workspace: Path) -> None:
        session = _make_pt_session(tmp_workspace)
        handler = _find_enter_handler(session)
        buf = Buffer()
        buf.insert_text("line1")
        submitted: list[str] = []
        buf.accept_handler = lambda b: submitted.append(b.text) or True
        handler(_FakeEnterEvent(buf, deque(["still-queued"])))
        assert buf.text == "line1\n"
        assert submitted == []

    def test_standalone_enter_with_empty_queue_submits(self, tmp_workspace: Path) -> None:
        session = _make_pt_session(tmp_workspace)
        handler = _find_enter_handler(session)
        buf = Buffer()
        buf.insert_text("hello")
        submitted: list[str] = []
        buf.accept_handler = lambda b: submitted.append(b.text) or True
        handler(_FakeEnterEvent(buf, deque()))
        assert "\n" not in buf.text
        assert submitted == ["hello"]

    def test_full_multiline_paste_with_leaked_enters_submits_as_one_block(self, tmp_workspace: Path) -> None:
        """End-to-end simulation of the actual bug: a 3-line paste whose
        embedded newlines leak through individually must still be preserved
        as ONE multi-line prompt, not split into 3 premature submissions."""
        session = _make_pt_session(tmp_workspace)
        handler = _find_enter_handler(session)
        buf = Buffer()
        submitted: list[str] = []
        buf.accept_handler = lambda b: submitted.append(b.text) or True

        buf.insert_text("line1")
        handler(_FakeEnterEvent(buf, deque(["x"])))  # leaked newline #1, more queued
        buf.insert_text("line2")
        handler(_FakeEnterEvent(buf, deque(["y"])))  # leaked newline #2, more queued
        buf.insert_text("line3")
        handler(_FakeEnterEvent(buf, deque()))  # the real, final Enter

        assert submitted == ["line1\nline2\nline3"]

    def test_open_completion_popup_still_takes_precedence(self, tmp_workspace: Path) -> None:
        """Accepting a completion (e.g. a slash command) must win even if
        keys happen to be queued behind this Enter."""
        session = _make_pt_session(tmp_workspace)
        handler = _find_enter_handler(session)
        buf = Buffer()
        buf.insert_text("/mo")
        buf.complete_state = CompletionState(original_document=Document("/mo"))
        submitted: list[str] = []
        buf.accept_handler = lambda b: submitted.append(b.text) or True
        handler(_FakeEnterEvent(buf, deque(["queued"])))
        assert buf.complete_state is None
        assert submitted == []


# ---------------------------------------------------------------------------
# Windows Ctrl+V clipboard paste binding
# ---------------------------------------------------------------------------

class TestWindowsCtrlVPaste:
    """prompt_toolkit switches the Windows console to raw mode, which turns
    OFF conhost's own Ctrl+V paste handling — the keystroke arrives as a bare
    c-v keypress and, unbound, did NOTHING (the real 'can't paste at all in
    the REPL' symptom on the user's box). A c-v binding reading the system
    clipboard via ctypes fixes it; POSIX terminals handle paste themselves,
    so the binding is Windows-only."""

    def test_binding_registered_on_windows(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_IS_WINDOWS", True)
        session = cli_mod._make_pt_session(tmp_path)
        assert session is not None
        assert any(
            "c-v" in str(getattr(b, "keys", "")) for b in session.key_bindings.bindings
        ), "c-v binding missing on Windows"

    def test_binding_absent_on_posix(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_IS_WINDOWS", False)
        session = cli_mod._make_pt_session(tmp_path)
        assert session is not None
        assert not any(
            "c-v" in str(getattr(b, "keys", "")) for b in session.key_bindings.bindings
        )

    def test_normalize_pasted_text_converts_crlf(self) -> None:
        from coding_agent.interface.cli import _normalize_pasted_text
        assert _normalize_pasted_text("a\r\nb\rc\nd") == "a\nb\nc\nd"

    def test_paste_handler_inserts_clipboard_into_real_buffer(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_IS_WINDOWS", True)
        monkeypatch.setattr(cli_mod, "_read_windows_clipboard", lambda: "line1\r\nline2 中文")
        session = cli_mod._make_pt_session(tmp_path)
        binding = next(
            b for b in session.key_bindings.bindings if "c-v" in str(getattr(b, "keys", ""))
        )
        buf = Buffer(document=Document(""), multiline=True)

        class _FakePasteEvent:
            current_buffer = buf

        binding.handler(_FakePasteEvent())
        assert buf.text == "line1\nline2 中文"

    def test_paste_handler_noop_on_empty_clipboard(self, tmp_path: Path, monkeypatch) -> None:
        from coding_agent.interface import cli as cli_mod
        monkeypatch.setattr(cli_mod, "_IS_WINDOWS", True)
        monkeypatch.setattr(cli_mod, "_read_windows_clipboard", lambda: "")
        session = cli_mod._make_pt_session(tmp_path)
        binding = next(
            b for b in session.key_bindings.bindings if "c-v" in str(getattr(b, "keys", ""))
        )
        buf = Buffer(document=Document("keep"), multiline=True)

        class _FakePasteEvent:
            current_buffer = buf

        binding.handler(_FakePasteEvent())
        assert buf.text == "keep"

    def test_read_windows_clipboard_returns_empty_off_windows(self) -> None:
        from coding_agent.interface.cli import _read_windows_clipboard
        # ctypes.windll doesn't exist on POSIX — the guard must swallow it.
        assert _read_windows_clipboard() == ""
