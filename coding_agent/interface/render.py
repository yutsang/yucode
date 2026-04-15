"""Terminal rendering for a Claude Code-like interactive experience.

Provides: startup banner, braille spinner, tool call cards with Unicode
box-drawing, streaming text display, color theming, and status lines.

Modeled after claw-code-main/rust/crates/claw-cli/src/render.rs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ---- Windows console support ------------------------------------------------

_IS_WIN = os.name == "nt"


def _enable_windows_vt() -> None:
    """Enable ANSI/VT escape processing on Windows 10+ consoles.

    Without this, escape sequences print literally on legacy cmd.exe.
    No-op on non-Windows and when VT mode is already active.
    """
    if not _IS_WIN:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for std_handle_id in (-11, -12):   # STD_OUTPUT_HANDLE, STD_ERROR_HANDLE
            handle = kernel32.GetStdHandle(std_handle_id)
            mode = ctypes.c_ulong()
            if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
                kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


_enable_windows_vt()

# Unicode glyphs (braille, dingbats, …) are safe when:
#   - not Windows at all, OR
#   - running inside Windows Terminal (WT_SESSION is set), OR
#   - Python UTF-8 mode / PYTHONUTF8, OR
#   - the console code page is UTF-8 (cp65001)
_enc = (getattr(sys.stdout, "encoding", None) or "").lower().replace("-", "")
_UNICODE_SAFE = (
    not _IS_WIN
    or bool(os.environ.get("WT_SESSION"))
    or bool(os.environ.get("PYTHONUTF8"))
    or _enc in ("utf8", "utf-8", "65001")
)

# Symbols that may not render on legacy Windows consoles (cmd.exe / cp1252)
_SYM_OK    = "✔" if _UNICODE_SAFE else "+"
_SYM_ERR   = "✘" if _UNICODE_SAFE else "x"
_SYM_WARN  = "⚠" if _UNICODE_SAFE else "!"
_SYM_INFO  = "ℹ" if _UNICODE_SAFE else "i"
_SYM_ARROW = "▸" if _UNICODE_SAFE else ">"


def _get_version() -> str:
    try:
        from .. import __version__
        return __version__
    except Exception:
        return "0.3.0"


# ---- Color theme ------------------------------------------------------------

_IS_TTY = hasattr(sys.stdout, "isatty") and sys.stdout.isatty()
_NO_COLOR = bool(os.environ.get("NO_COLOR"))
_COLOR = _IS_TTY and not _NO_COLOR


def _esc(code: str) -> str:
    return f"\x1b[{code}" if _COLOR else ""


RESET = _esc("0m")
BOLD = _esc("1m")
DIM = _esc("2m")
ITALIC = _esc("3m")
UNDERLINE = _esc("4m")

FG_CYAN = _esc("38;5;45m")
FG_MAGENTA = _esc("38;5;213m")
FG_YELLOW = _esc("33m")
FG_GREEN = _esc("32m")
FG_RED = _esc("31m")
FG_BLUE = _esc("34m")
FG_GRAY = _esc("38;5;245m")
FG_WHITE = _esc("37m")
FG_ORANGE = _esc("38;5;208m")

BG_DARK = _esc("48;5;236m")


@dataclass
class ColorTheme:
    brand: str = FG_CYAN
    brand_accent: str = FG_MAGENTA
    heading: str = FG_CYAN
    emphasis: str = FG_YELLOW
    link: str = FG_BLUE
    success: str = FG_GREEN
    error: str = FG_RED
    warning: str = FG_ORANGE
    muted: str = FG_GRAY
    spinner_active: str = FG_CYAN
    tool_border: str = FG_GRAY
    tool_name: str = FG_CYAN
    prompt_you: str = FG_CYAN
    prompt_agent: str = FG_MAGENTA


THEME = ColorTheme()


# ---- Spinner ----------------------------------------------------------------

# Braille dots (U+28xx) are not in Consolas; use simple ASCII on legacy Windows.
_SPINNER_FRAMES = (
    ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
    if _UNICODE_SAFE else
    ["|", "/", "-", "\\"]
)


class Spinner:
    """Simple single-line spinner for non-rolling contexts (e.g. 'Thinking')."""

    FRAMES = _SPINNER_FRAMES
    INTERVAL = 0.08

    def __init__(self, label: str = "Thinking") -> None:
        self._label = label
        self._frame = 0
        self._running = False
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        self._running = True
        self._frame = 0
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()

    def stop(self, *, success: bool = True, result_text: str = "") -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._clear_line()
        if result_text:
            sys.stderr.write(result_text + "\n")
        elif success:
            sys.stderr.write(f"  {THEME.success}{_SYM_OK}{RESET} {DIM}{self._label}{RESET}\n")
        else:
            sys.stderr.write(f"  {THEME.error}{_SYM_ERR}{RESET} {DIM}{self._label}{RESET}\n")
        sys.stderr.flush()

    def update_label(self, label: str) -> None:
        with self._lock:
            self._label = label

    def _spin(self) -> None:
        while self._running:
            frame = self.FRAMES[self._frame % len(self.FRAMES)]
            self._frame += 1
            with self._lock:
                label = self._label
            self._clear_line()
            sys.stderr.write(f"  {THEME.spinner_active}{frame}{RESET} {DIM}{label}...{RESET}")
            sys.stderr.flush()
            time.sleep(self.INTERVAL)

    @staticmethod
    def _clear_line() -> None:
        sys.stderr.write("\r\x1b[K")
        sys.stderr.flush()


class ProgressDisplay:
    """Two-line rolling progress window.

    At most two lines are visible at any time:
      Line 1 (done):  ✔ web_search  3 results
      Line 2 (doing): ⠹ web_fetch: https://example.com…

    When the current tool finishes and a new one starts, the old "doing"
    line becomes the new "done" line and the new tool takes its place.
    """

    INTERVAL = 0.08

    def __init__(self) -> None:
        self._done_line: str = ""
        self._doing_label: str = ""
        self._frame: int = 0
        self._spinning: bool = False
        self._lines_on_screen: int = 0
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start_tool(self, label: str) -> None:
        """Begin spinning for a new tool."""
        with self._lock:
            self._doing_label = label
            self._frame = 0
            self._spinning = True
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def finish_tool(self, result_line: str) -> None:
        """Mark current tool as done.  Its result becomes the new 'done' line."""
        with self._lock:
            self._spinning = False
            self._done_line = result_line
            self._doing_label = ""
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._redraw()

    def set_thinking(self, label: str = "Thinking") -> None:
        """Show a 'Thinking' spinner (no done line above)."""
        with self._lock:
            self._doing_label = label
            self._frame = 0
            self._spinning = True
        if not self._thread or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._animate, daemon=True)
            self._thread.start()

    def stop(self) -> None:
        """Stop all animation and clear progress lines from screen."""
        with self._lock:
            self._spinning = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._erase()
        self._done_line = ""
        self._doing_label = ""

    def _animate(self) -> None:
        while True:
            with self._lock:
                if not self._spinning:
                    break
            self._redraw()
            time.sleep(self.INTERVAL)

    def _redraw(self) -> None:
        with self._lock:
            lines: list[str] = []
            if self._done_line:
                lines.append(self._done_line)
            if self._spinning and self._doing_label:
                frame = _SPINNER_FRAMES[self._frame % len(_SPINNER_FRAMES)]
                self._frame += 1
                lines.append(
                    f"  {THEME.spinner_active}{frame}{RESET} "
                    f"{DIM}{self._doing_label}...{RESET}"
                )
            elif not self._spinning and self._done_line:
                pass

        self._erase()
        for line in lines:
            sys.stderr.write(line + "\n")
        sys.stderr.flush()
        self._lines_on_screen = len(lines)

    def _erase(self) -> None:
        if self._lines_on_screen > 0:
            sys.stderr.write(
                f"\x1b[{self._lines_on_screen}A"
                f"\x1b[J"
            )
            sys.stderr.flush()
            self._lines_on_screen = 0


# ---- Compact tool display (single-line dynamic) ----------------------------

_PATH_TOOLS = frozenset({
    "read_file", "Read", "write_file", "Write", "edit_file", "edit",
    "read_excel_sheet", "list_excel_sheets", "excel_to_json",
    "read_word_text", "read_word_paragraphs", "read_pptx", "read_pdf_text",
})


def compact_tool_start_label(name: str, arguments: str) -> str:
    """One-line label for a tool call, used as spinner text."""
    import json as _json
    try:
        parsed = _json.loads(arguments)
    except Exception:
        parsed = {}

    icon = _TOOL_ICONS.get(name, "⚡")

    if name in _PATH_TOOLS:
        return f"{icon} {name}: {parsed.get('path', '?')}"
    if name in ("bash", "Bash"):
        return f"{icon} $ {_truncate(parsed.get('command', '?'), 55)}"
    if name in ("web_search",):
        return f"{icon} {name}: {parsed.get('query', '?')}"
    if name in ("web_fetch",):
        return f"{icon} {name}: {_truncate(parsed.get('url', '?'), 55)}"
    if name in ("list_directory",):
        return f"{icon} ls: {parsed.get('path', '.')}"
    if name in ("glob_search",):
        return f"{icon} glob: {parsed.get('pattern', '?')}"
    if name in ("grep_search",):
        return f"{icon} grep: {parsed.get('pattern', '?')}"
    if name == "agent":
        return f"{icon} agent: {_truncate(str(parsed.get('description', parsed.get('prompt', '?'))), 50)}"
    return f"{icon} {name}"


def compact_tool_result_line(name: str, content: str, *, is_error: bool = False) -> str:
    """Single finished line: ✔/✘ + tool name + compact summary."""
    summary = _summarize_tool_result(name, content, is_error)
    if is_error:
        return f"  {THEME.error}{_SYM_ERR}{RESET} {name} {DIM}{summary}{RESET}"
    return f"  {THEME.success}{_SYM_OK}{RESET} {name} {DIM}{summary}{RESET}"


# ---- Startup banner ---------------------------------------------------------

def startup_banner(
    workspace: Path,
    model: str,
    provider: str,
    permission_mode: str,
    session_info: str = "",
) -> str:
    cwd = str(workspace)
    home = str(Path.home())
    if cwd.startswith(home):
        cwd = "~" + cwd[len(home):]

    branch = _git_branch(workspace)
    branch_display = f" ({branch})" if branch else ""

    lines = [
        "",
        f"  {BOLD}{THEME.brand}╭─ YuCode{RESET} {DIM}· v{_get_version()}{RESET}",
        f"  {THEME.brand}│{RESET}",
        f"  {THEME.brand}│{RESET}  {DIM}cwd{RESET}         {cwd}{branch_display}",
        f"  {THEME.brand}│{RESET}  {DIM}model{RESET}       {provider}/{model}",
        f"  {THEME.brand}│{RESET}  {DIM}permissions{RESET}  {permission_mode}",
    ]

    if session_info:
        lines.append(f"  {THEME.brand}│{RESET}  {DIM}session{RESET}     {session_info}")

    yucode_md = workspace / "YUCODE.md"
    claw_md = workspace / "CLAW.md"
    if yucode_md.is_file():
        lines.append(f"  {THEME.brand}│{RESET}  {DIM}memory{RESET}      YUCODE.md loaded")
    elif claw_md.is_file():
        lines.append(f"  {THEME.brand}│{RESET}  {DIM}memory{RESET}      CLAW.md loaded")

    lines.extend([
        f"  {THEME.brand}│{RESET}",
        f"  {THEME.brand}╰─{RESET} {DIM}Type {THEME.prompt_you}/help{RESET}{DIM} for commands, Tab to complete{RESET}",
        "",
    ])
    return "\n".join(lines)


def _git_branch(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, cwd=str(workspace), check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except FileNotFoundError:
        return ""


# ---- Tool call cards --------------------------------------------------------

_TOOL_ICONS: dict[str, str] = {
    "read_file": "📄",
    "write_file": "✏️ ",
    "edit_file": "🔧",
    "glob_search": "🔍",
    "grep_search": "🔎",
    "list_directory": "📁",
    "bash": "💻",
    "web_fetch": "🌐",
    "web_search": "🔍",
    "agent": "🤖",
    "edit_notebook": "📓",
    "load_skill": "📚",
    "todo_write": "📋",
    "sleep": "💤",
    "read_excel_sheet": "📊",
    "write_excel_cell": "📊",
    "list_excel_sheets": "📊",
    "excel_to_json": "📊",
    "read_word_text": "📝",
    "read_word_paragraphs": "📝",
    "write_word": "📝",
    "append_word": "📝",
    "read_pptx": "📽️ ",
    "write_pptx": "📽️ ",
    "read_pdf_text": "📕",
}


def format_tool_call_start(name: str, arguments: str) -> str:
    try:
        parsed = __import__("json").loads(arguments)
    except Exception:
        parsed = {}

    icon = _TOOL_ICONS.get(name, "⚡")
    detail = _format_tool_detail(name, parsed, arguments)

    name_display = f"{BOLD}{THEME.tool_name}{name}{RESET}"
    border_len = len(name) + 4
    top_border = "─" * border_len

    lines = [
        f"  {THEME.tool_border}╭─ {name_display} {THEME.tool_border}─╮{RESET}",
        f"  {THEME.tool_border}│{RESET} {icon} {detail}",
        f"  {THEME.tool_border}╰{top_border}╯{RESET}",
    ]
    return "\n".join(lines)


def format_tool_result(name: str, content: str, *, is_error: bool = False) -> str:
    if is_error:
        icon = f"{THEME.error}{_SYM_ERR}{RESET}"
        label = f"{THEME.error}error{RESET}"
    else:
        icon = f"{THEME.success}{_SYM_OK}{RESET}"
        label = f"{THEME.success}done{RESET}"

    summary = _summarize_tool_result(name, content, is_error)
    return f"  {icon} {label} {DIM}({name}){RESET} {DIM}{summary}{RESET}"


def _summarize_tool_result(name: str, content: str, is_error: bool) -> str:
    """Produce a compact, human-readable one-liner from tool output."""
    import json as _json

    if is_error:
        try:
            parsed = _json.loads(content)
            err_msg = parsed.get("error", content)
            return _truncate(str(err_msg), 80)
        except Exception:
            return _truncate(content, 80)

    if name in ("web_search",):
        try:
            hits = _json.loads(content)
            if isinstance(hits, list):
                if not hits:
                    return "no results"
                titles = [h.get("title", "?") for h in hits[:3] if isinstance(h, dict)]
                suffix = f" (+{len(hits) - 3} more)" if len(hits) > 3 else ""
                return " · ".join(_truncate(t, 35) for t in titles) + suffix
        except Exception:
            pass
        return _truncate(content, 80)

    if name in ("web_fetch",):
        try:
            parsed = _json.loads(content)
            status = parsed.get("status", "?")
            byte_size = parsed.get("bytes", 0)
            duration = parsed.get("duration_ms", 0)
            size_str = f"{byte_size // 1024}KB" if byte_size > 1024 else f"{byte_size}B"
            return f"{status} · {size_str} · {duration}ms"
        except Exception:
            pass
        return _truncate(content, 80)

    if name in ("read_file",):
        lines = content.count("\n")
        return f"{lines + 1} lines"

    if name in ("write_file",):
        return _truncate(content, 60)

    if name in ("edit_file",):
        return _truncate(content, 60)

    if name in ("bash",):
        try:
            parsed = _json.loads(content)
            rc = parsed.get("returncode", "?")
            stdout = parsed.get("stdout", "")
            stderr = parsed.get("stderr", "")
            out_lines = stdout.strip().count("\n") + 1 if stdout.strip() else 0
            status = f"exit {rc}"
            if out_lines:
                status += f" · {out_lines} lines"
            if stderr.strip():
                status += f" · stderr: {_truncate(stderr.strip(), 40)}"
            return status
        except Exception:
            pass
        return _truncate(content, 80)

    if name in ("list_directory",):
        try:
            parsed = _json.loads(content)
            n = parsed.get("count", len(parsed.get("entries", [])))
            return f"{n} entries"
        except Exception:
            pass
        return _truncate(content, 80)

    if name in ("glob_search",):
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, list):
                n = len(parsed)
                if n == 0:
                    return "no matches"
                if n <= 3:
                    return ", ".join(parsed)
                return f"{n} matches  ({parsed[0]}, …)"
            if isinstance(parsed, dict):
                # fuzzy-fallback format: {"matches": [], "hint": "...", "similar": [...]}
                n = len(parsed.get("matches", []))
                if n == 0:
                    sim = parsed.get("similar", [])
                    if sim:
                        first_pat = sim[0].get("pattern", "?")
                        return f"no matches — try: {first_pat}"
                    return "no matches"
                return f"{n} matches"
        except Exception:
            pass
        return _truncate(content, 80)

    if name in ("grep_search",):
        lines = content.strip().count("\n") + 1 if content.strip() else 0
        return f"{lines} matching lines"

    if name in ("read_excel_sheet", "excel_read_sheet", "list_sheets"):
        try:
            parsed = _json.loads(content)
            if isinstance(parsed, dict):
                rows = parsed.get("rows", [])
                return f"{len(rows)} rows"
        except Exception:
            pass

    if name in ("read_word_text", "read_docx_full_text"):
        chars = len(content)
        return f"{chars:,} chars"

    return _truncate(content.replace("\n", " ").strip(), 60)


def _format_tool_detail(name: str, parsed: dict[str, Any], raw: str) -> str:
    if name in ("read_file", "Read"):
        path = parsed.get("path", "?")
        parts = [f"{DIM}Reading {path}"]
        if parsed.get("offset"):
            parts.append(f" (from line {parsed['offset']})")
        if parsed.get("limit"):
            parts.append(f" [{parsed['limit']} lines]")
        return "".join(parts) + f"{RESET}"

    if name in ("write_file", "Write"):
        path = parsed.get("path", "?")
        content = parsed.get("content", "")
        lines = content.count("\n") + 1 if content else 0
        return f"{THEME.success}Writing {path}{RESET} {DIM}({lines} lines){RESET}"

    if name in ("edit_file", "edit"):
        path = parsed.get("path", "?")
        return f"{THEME.emphasis}Editing {path}{RESET}"

    if name in ("bash", "Bash"):
        cmd = parsed.get("command", raw)
        if isinstance(cmd, str):
            cmd = _truncate(cmd, 80)
        return f"{THEME.warning}$ {cmd}{RESET}"

    if name in ("list_directory",):
        path = parsed.get("path", ".")
        return f"{DIM}Listing {path}{RESET}"

    if name in ("glob_search",):
        pattern = parsed.get("pattern", "?")
        return f"{DIM}Glob: {pattern}{RESET}"

    if name in ("grep_search",):
        pattern = parsed.get("pattern", "?")
        return f"{DIM}Grep: {pattern}{RESET}"

    if name in ("web_fetch",):
        url = parsed.get("url", "?")
        return f"{DIM}Fetching {_truncate(url, 60)}{RESET}"

    if name in ("web_search",):
        query = parsed.get("query", parsed.get("search_term", "?"))
        return f"{DIM}Searching: {query}{RESET}"

    if name == "agent":
        desc = parsed.get("description", parsed.get("prompt", "?"))
        return f"{DIM}Sub-agent: {_truncate(str(desc), 60)}{RESET}"

    if name in ("read_excel_sheet", "list_excel_sheets", "excel_to_json"):
        path = parsed.get("path", "?")
        sheet = parsed.get("sheet", "")
        sheet_note = f" [{sheet}]" if sheet else ""
        return f"{DIM}Reading {path}{sheet_note}{RESET}"

    if name in ("write_excel_cell",):
        path = parsed.get("path", "?")
        cell = parsed.get("cell", "?")
        return f"{DIM}Writing {path} cell {cell}{RESET}"

    if name in ("read_word_text", "read_word_paragraphs"):
        path = parsed.get("path", "?")
        return f"{DIM}Reading {path}{RESET}"

    if name in ("write_word",):
        path = parsed.get("path", "?")
        paras = len(parsed.get("paragraphs", []))
        return f"{THEME.success}Creating {path}{RESET} {DIM}({paras} paragraphs){RESET}"

    if name in ("append_word",):
        path = parsed.get("path", "?")
        return f"{DIM}Appending to {path}{RESET}"

    if name in ("read_pptx",):
        path = parsed.get("path", "?")
        return f"{DIM}Reading {path}{RESET}"

    if name in ("write_pptx",):
        path = parsed.get("path", "?")
        slides = len(parsed.get("slides", []))
        return f"{THEME.success}Creating {path}{RESET} {DIM}({slides} slides){RESET}"

    if name in ("read_pdf_text",):
        path = parsed.get("path", "?")
        return f"{DIM}Reading {path}{RESET}"

    return f"{DIM}{_truncate(raw, 80)}{RESET}"


# ---- Assistant response display ---------------------------------------------

def format_assistant_text(text: str) -> str:
    if not _COLOR:
        return text
    return _render_markdown_ansi(text)


def _render_markdown_ansi(text: str) -> str:
    lines = text.split("\n")
    output: list[str] = []
    in_code_block = False

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("```"):
            in_code_block = not in_code_block
            if in_code_block:
                lang = stripped[3:].strip()
                output.append(f"  {THEME.muted}┌{'─' * 60}{RESET}")
                if lang:
                    output.append(f"  {THEME.muted}│ {DIM}{lang}{RESET}")
            else:
                output.append(f"  {THEME.muted}└{'─' * 60}{RESET}")
            continue

        if in_code_block:
            output.append(f"  {THEME.muted}│{RESET} {line}")
            continue

        if stripped.startswith("# "):
            output.append(f"\n  {BOLD}{THEME.heading}{stripped[2:]}{RESET}")
        elif stripped.startswith("## "):
            output.append(f"\n  {BOLD}{THEME.heading}{stripped[3:]}{RESET}")
        elif stripped.startswith("### "):
            output.append(f"\n  {THEME.heading}{stripped[4:]}{RESET}")
        elif stripped.startswith("- ") or stripped.startswith("* "):
            bullet_content = _inline_format(stripped[2:])
            output.append(f"  • {bullet_content}")
        elif stripped.startswith(("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.")):
            output.append(f"  {_inline_format(stripped)}")
        elif stripped == "---" or stripped == "***":
            cols = min(shutil.get_terminal_size().columns - 4, 60)
            output.append(f"  {THEME.muted}{'─' * cols}{RESET}")
        elif stripped:
            output.append(f"  {_inline_format(stripped)}")
        else:
            output.append("")

    return "\n".join(output)


_RE_INLINE_CODE = __import__("re").compile(r"`([^`]+)`")
_RE_BOLD = __import__("re").compile(r"\*\*([^*]+)\*\*")
_RE_ITALIC = __import__("re").compile(r"(?<!\*)\*([^*]+)\*(?!\*)")


def _inline_format(text: str) -> str:
    text = _RE_INLINE_CODE.sub(rf"{THEME.emphasis}\1{RESET}", text)
    text = _RE_BOLD.sub(rf"{BOLD}\1{RESET}", text)
    text = _RE_ITALIC.sub(rf"{ITALIC}\1{RESET}", text)
    return text


# ---- Status / info lines ---------------------------------------------------

def format_status_line(
    tokens_in: int,
    tokens_out: int,
    iteration: int,
    elapsed_ms: float = 0,
) -> str:
    parts = [
        f"{DIM}tokens:{RESET} {tokens_in}↓ {tokens_out}↑",
        f"{DIM}iter:{RESET} {iteration}",
    ]
    if elapsed_ms > 0:
        secs = elapsed_ms / 1000
        parts.append(f"{DIM}time:{RESET} {secs:.1f}s")
    return f"  {THEME.muted}│{RESET} " + f" {THEME.muted}·{RESET} ".join(parts)


def format_cost_summary(usage: dict[str, int]) -> str:
    inp = usage.get("input_tokens", 0)
    out = usage.get("output_tokens", 0)
    cache_create = usage.get("cache_creation_input_tokens", 0)
    cache_read = usage.get("cache_read_input_tokens", 0)
    total = inp + out + cache_create + cache_read

    lines = [
        f"  {THEME.muted}╭─ Token usage ──────────────────╮{RESET}",
        f"  {THEME.muted}│{RESET}  input       {inp:>8,}",
        f"  {THEME.muted}│{RESET}  output      {out:>8,}",
    ]
    if cache_create:
        lines.append(f"  {THEME.muted}│{RESET}  cache write {cache_create:>8,}")
    if cache_read:
        lines.append(f"  {THEME.muted}│{RESET}  cache read  {cache_read:>8,}")
    lines.extend([
        f"  {THEME.muted}│{RESET}  {BOLD}total{RESET}        {total:>8,}",
        f"  {THEME.muted}╰────────────────────────────────╯{RESET}",
    ])
    return "\n".join(lines)


# ---- Memory display ---------------------------------------------------------

def format_memory_display(
    workspace: Path,
    instruction_files: list[Path],
    sessions: list[dict[str, Any]],
    skills: list[Any],
    estimated_tokens: int,
    session_messages: int,
    compaction_count: int = 0,
    metrics: dict[str, Any] | None = None,
) -> str:
    lines = [
        f"  {BOLD}{THEME.heading}Memory & Context{RESET}",
        f"  {THEME.muted}{'─' * 40}{RESET}",
        "",
    ]

    lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Working Memory{RESET}")
    lines.append(f"    Messages in session:  {session_messages}")
    lines.append(f"    Estimated tokens:     {estimated_tokens:,}")
    if compaction_count:
        lines.append(f"    Compactions done:     {compaction_count}")
    lines.append("")

    lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Project Memory{RESET} (instruction files)")
    if instruction_files:
        for f in instruction_files:
            try:
                rel = f.relative_to(workspace)
            except ValueError:
                rel = f
            size = f.stat().st_size if f.exists() else 0
            lines.append(f"    📄 {rel} ({size:,} bytes)")
    else:
        lines.append(f"    {DIM}No instruction files found.{RESET}")
        lines.append(f"    {DIM}Create YUCODE.md to add project context.{RESET}")
    lines.append("")

    lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Skills{RESET}")
    if skills:
        for s in skills:
            name = getattr(s, "name", str(s))
            desc = getattr(s, "description", "")
            lines.append(f"    📚 {name}" + (f" — {desc}" if desc else ""))
    else:
        lines.append(f"    {DIM}No skills discovered.{RESET}")
    lines.append("")

    lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Saved Sessions{RESET}")
    if sessions:
        for s in sessions[:5]:
            sid = s.get("id", "?")
            model = s.get("model", "?")
            msgs = s.get("message_count", 0)
            lines.append(f"    💾 {sid}  model={model}  msgs={msgs}")
        if len(sessions) > 5:
            lines.append(f"    {DIM}... and {len(sessions) - 5} more{RESET}")
    else:
        lines.append(f"    {DIM}No saved sessions.{RESET}")
    lines.append("")

    if metrics:
        tools = metrics.get("tools", {})
        if tools:
            lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Tool Usage (this session){RESET}")
            for name, m in sorted(tools.items(), key=lambda x: x[1].get("call_count", 0), reverse=True)[:8]:
                count = m.get("call_count", 0)
                avg = m.get("avg_duration_ms", 0)
                errs = m.get("error_count", 0)
                err_note = f" {THEME.error}({errs} errors){RESET}" if errs else ""
                lines.append(f"    ⚡ {name}: {count}× avg {avg:.0f}ms{err_note}")
            lines.append("")

        security = metrics.get("security_events", [])
        if security:
            lines.append(f"  {THEME.brand}{_SYM_ARROW}{RESET} {BOLD}Security Events{RESET}")
            for ev in security[-5:]:
                lines.append(f"    🛡️  {ev.get('event_type', '?')} → {ev.get('tool_name', '?')}: {ev.get('detail', '')[:60]}")
            lines.append("")

    return "\n".join(lines)


# ---- Prompt -----------------------------------------------------------------

def user_prompt() -> str:
    return f"{THEME.prompt_you}{BOLD}you ›{RESET} "


def agent_label() -> str:
    return f"\n{THEME.prompt_agent}{BOLD}yucode{RESET}\n"


# ---- Utilities --------------------------------------------------------------

def _truncate(text: str, max_chars: int) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def hr(char: str = "─", width: int | None = None) -> str:
    w = width or min(shutil.get_terminal_size().columns - 4, 60)
    return f"  {THEME.muted}{char * w}{RESET}"


def section_header(title: str) -> str:
    return f"\n  {BOLD}{THEME.heading}{title}{RESET}\n  {THEME.muted}{'─' * (len(title) + 2)}{RESET}"


def success(msg: str) -> str:
    return f"  {THEME.success}{_SYM_OK}{RESET} {msg}"


def error(msg: str) -> str:
    return f"  {THEME.error}{_SYM_ERR}{RESET} {msg}"


def warning(msg: str) -> str:
    return f"  {THEME.warning}{_SYM_WARN}{RESET} {msg}"


def info(msg: str) -> str:
    return f"  {THEME.brand}{_SYM_INFO}{RESET} {msg}"
