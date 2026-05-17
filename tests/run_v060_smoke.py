"""Run the 10-prompt v0.6.0 smoke test and write a TXT report.

Two modes:
  - Default (fast, offline):  python tests/run_v060_smoke.py
        Verifies each subsystem at the code level (no provider call).
        Catches regressions in the tools / memory / prompt-wiring
        layers in ~1 second. ~9 of 10 prompts have a meaningful
        offline assertion; #9 (multi-agent memory) needs the LLM.

  - --live mode:  python tests/run_v060_smoke.py --live
        Actually sends each prompt through the configured agent
        (uses your default yucode provider + model). Slow; needs a
        valid API key. Captures real tool-call traces.

The report is written to results.txt next to this script by default,
or wherever you pass --out.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


@dataclass
class CheckResult:
    name: str
    status: str          # PASS / FAIL / SKIP
    detail: str = ""
    duration_s: float = 0.0
    events: list[str] = field(default_factory=list)


@contextlib.contextmanager
def _redirect_home(tmp: str):
    """Point Path.home() at *tmp* on every OS.

    Path.home() reads HOME on POSIX but USERPROFILE on Windows (with
    HOMEDRIVE+HOMEPATH as a fallback). Setting just HOME silently fails
    on Windows and tests start writing to the real user dir.
    """
    keys = ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["HOME"] = tmp
        os.environ["USERPROFILE"] = tmp
        # Split tmp into drive + path for HOMEDRIVE/HOMEPATH (Windows legacy)
        drive, path = os.path.splitdrive(tmp)
        if drive:
            os.environ["HOMEDRIVE"] = drive
            os.environ["HOMEPATH"] = path or "\\"
        else:
            # Non-Windows path; remove HOMEDRIVE/HOMEPATH to avoid confusing
            # Path.home() if they were set in the parent env
            os.environ.pop("HOMEDRIVE", None)
            os.environ.pop("HOMEPATH", None)
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---- prompt #1 — time-sensitive grounding ----------------------------------

def check_01_time_sensitive(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.core.runtime import (
        _ToolObservations,
        _check_final_answer_grounding,
        _is_time_sensitive_prompt,
    )

    prompt = "香港站市區預辦登機現在有哪些航空公司？"

    if not _is_time_sensitive_prompt(prompt):
        return CheckResult("01 time-sensitive detection", "FAIL",
                           f"prompt not flagged as time-sensitive: {prompt!r}",
                           time.monotonic() - started)

    # Simulate: model returned an answer mentioning Dragonair WITHOUT web_search
    obs = _ToolObservations()
    violation = _check_final_answer_grounding(
        obs, "包括國泰、港龍、長榮",
        is_weak_investigation=False, is_time_sensitive=True,
    )
    if violation is None or violation.reason != "time_sensitive_no_web_search":
        return CheckResult("01 time-sensitive detection", "FAIL",
                           f"expected grounding violation, got {violation}",
                           time.monotonic() - started)

    if live:
        result = _run_live_prompt(prompt)
        if result["error"]:
            return CheckResult("01 time-sensitive grounding (LIVE)", "FAIL",
                               result["error"], time.monotonic() - started)
        text = result["final_text"]
        tools = result["tool_names"]
        details: list[str] = [f"tools={tools}", f"answer={text[:120]}…"]
        if "港龍" in text or "Dragonair" in text:
            return CheckResult("01 time-sensitive grounding (LIVE)", "FAIL",
                               f"answer still mentions Dragonair: {text[:200]}",
                               time.monotonic() - started, events=details)
        if "web_search" not in tools and "web_fetch" not in tools:
            return CheckResult("01 time-sensitive grounding (LIVE)", "FAIL",
                               "no web_search / web_fetch called",
                               time.monotonic() - started, events=details)
        return CheckResult("01 time-sensitive grounding (LIVE)", "PASS",
                           "; ".join(details), time.monotonic() - started, events=details)

    return CheckResult("01 time-sensitive detection (offline)", "PASS",
                       "regex + grounding check fire as expected (use --live for end-to-end)",
                       time.monotonic() - started)


# ---- prompt #2 — Office / PDF routing --------------------------------------

def check_02_office_routing(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.config.settings import AppConfig
    from coding_agent.tools import ToolRegistry
    from coding_agent.tools.filesystem import _read_file

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        reg = ToolRegistry(workspace_root=ws, config=AppConfig())
        # Verify .xlsx routes to inspect_excel_sheets, not bash xxd
        (ws / "data.xlsx").write_bytes(b"PK\x03\x04 stub xlsx")
        try:
            _read_file(reg, {"path": "data.xlsx"})
        except ValueError as exc:
            msg = str(exc)
            if "inspect_excel_sheets" in msg or "read_excel_sheet" in msg:
                pass  # good
            else:
                return CheckResult("02 office routing", "FAIL",
                                   f"xlsx error didn't suggest Excel tool: {msg}",
                                   time.monotonic() - started)
        else:
            return CheckResult("02 office routing", "FAIL",
                               "xlsx read_file did not raise",
                               time.monotonic() - started)

        # Same for .pdf
        (ws / "doc.pdf").write_bytes(b"%PDF-1.4 stub")
        try:
            _read_file(reg, {"path": "doc.pdf"})
        except ValueError as exc:
            if "read_pdf_text" not in str(exc):
                return CheckResult("02 office routing", "FAIL",
                                   f"pdf error didn't suggest read_pdf_text: {exc}",
                                   time.monotonic() - started)

    return CheckResult("02 office routing", "PASS",
                       "xlsx → inspect_excel_sheets / read_excel_sheet; pdf → read_pdf_text",
                       time.monotonic() - started)


# ---- prompt #3 — Memory round-trip (write then recall) ---------------------

def check_03_memory_roundtrip(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.memory.store import MemoryStore

    with tempfile.TemporaryDirectory() as tmp, _redirect_home(tmp):
        ws = Path(tmp) / "ws"
        ws.mkdir()
        store = MemoryStore(ws)
        store.save("user-prefs", "Prefers Traditional Chinese, terse",
                   "user", "Always answer 繁中, no preamble.", scope="user")
        # Round-trip: fresh store, same workspace, must find it
        store2 = MemoryStore(ws)
        entry = store2.read("user-prefs", "user")
        if not entry:
            return CheckResult("03 memory round-trip", "FAIL",
                               "saved memory not retrievable from fresh MemoryStore",
                               time.monotonic() - started)
        if "繁中" not in entry.body:
            return CheckResult("03 memory round-trip", "FAIL",
                               f"body content mismatch: {entry.body[:80]}",
                               time.monotonic() - started)
        # Index loaded into prompt text
        idx = store2.load_indexes_text()
        if "user-prefs" not in idx:
            return CheckResult("03 memory round-trip", "FAIL",
                               f"prompt-loaded index missing entry: {idx[:200]}",
                               time.monotonic() - started)

    return CheckResult("03 memory round-trip", "PASS",
                       "save → fresh MemoryStore → read + index reload all OK",
                       time.monotonic() - started)


# ---- prompt #4 — Slash commands /remember /forget --------------------------

def check_04_slash_commands(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.interface.cli import (
        _handle_forget_command,
        _handle_remember_command,
    )
    from coding_agent.memory.store import MemoryStore

    with tempfile.TemporaryDirectory() as tmp, _redirect_home(tmp):
        ws = Path(tmp) / "ws"
        ws.mkdir()
        from io import StringIO
        captured = StringIO()
        real_stdout = sys.stdout
        sys.stdout = captured
        try:
            _handle_remember_command(ws, "-w project uses pnpm not npm")
        finally:
            sys.stdout = real_stdout
        workspace_memories = MemoryStore(ws).list("workspace")
        if not workspace_memories:
            return CheckResult("04 /remember + /forget", "FAIL",
                               "memory not saved to workspace scope",
                               time.monotonic() - started)
        name = workspace_memories[0].name
        captured = StringIO()
        sys.stdout = captured
        try:
            _handle_forget_command(ws, name)
        finally:
            sys.stdout = real_stdout
        if MemoryStore(ws).list("workspace"):
            return CheckResult("04 /remember + /forget", "FAIL",
                               "memory not deleted after /forget",
                               time.monotonic() - started)

    return CheckResult("04 /remember + /forget", "PASS",
                       "save → list → delete round-trip OK",
                       time.monotonic() - started)


# ---- prompt #5 — /init AGENTS.md generation --------------------------------

def check_05_init_agents(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.interface.init_workspace import detect_profile, write_agents_md

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        # Make it look like a Python project
        (ws / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
        (ws / "tests").mkdir()
        profile = detect_profile(ws)
        if "Python" not in profile.languages:
            return CheckResult("05 /init AGENTS.md", "FAIL",
                               f"Python not detected: {profile.languages}",
                               time.monotonic() - started)
        path, _ = write_agents_md(ws)
        content = path.read_text(encoding="utf-8")
        if "## Stack" not in content or "Python" not in content:
            return CheckResult("05 /init AGENTS.md", "FAIL",
                               f"AGENTS.md missing expected sections: {content[:200]}",
                               time.monotonic() - started)

    return CheckResult("05 /init AGENTS.md", "PASS",
                       "detect_profile + write_agents_md round-trip OK",
                       time.monotonic() - started)


# ---- prompt #6 — yucode init-memory ----------------------------------------

def check_06_init_memory(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.interface.memory_bootstrap import (
        USER_PROFILE_NAME,
        bootstrap_user_profile,
        gather_facts,
    )

    with tempfile.TemporaryDirectory() as tmp, _redirect_home(tmp):
        facts = gather_facts()
        if "os" not in facts:
            return CheckResult("06 init-memory", "FAIL",
                               f"gather_facts() missing OS: {facts}",
                               time.monotonic() - started)
        ws = Path(tmp) / "ws"
        ws.mkdir()
        entry, was_new = bootstrap_user_profile(ws)
        if not was_new or entry.name != USER_PROFILE_NAME:
            return CheckResult("06 init-memory", "FAIL",
                               f"bootstrap result unexpected: was_new={was_new}, name={entry.name}",
                               time.monotonic() - started)

    return CheckResult("06 init-memory", "PASS",
                       "gather_facts() + bootstrap_user_profile() OK",
                       time.monotonic() - started)


# ---- prompt #7 — Stale markers ---------------------------------------------

def check_07_staleness(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.memory.store import STALE_DAYS_THRESHOLD, MemoryStore

    with tempfile.TemporaryDirectory() as tmp, _redirect_home(tmp):
        ws = Path(tmp) / "ws"
        ws.mkdir()
        store = MemoryStore(ws)
        store.save("fresh", "fresh entry", "user", "body", scope="user")
        # Forge a stale entry
        stale_path = store.root_for("user") / "stale.md"
        stale_path.write_text(
            "---\nname: stale\ndescription: ancient\n"
            "saved_at: 2020-01-01\nmetadata:\n  type: project\n---\n\nbody\n",
            encoding="utf-8",
        )
        text = store.load_indexes_text()
        if "[stale: " not in text:
            return CheckResult("07 staleness markers", "FAIL",
                               f"no [stale: marker in index: {text[:300]}",
                               time.monotonic() - started)
        # Fresh entry must NOT have stale marker
        for line in text.splitlines():
            if line.startswith("- `fresh`") and "[stale" in line:
                return CheckResult("07 staleness markers", "FAIL",
                                   f"fresh entry incorrectly marked stale: {line}",
                                   time.monotonic() - started)

    return CheckResult("07 staleness markers", "PASS",
                       f"stale > {STALE_DAYS_THRESHOLD}d entries marked; fresh entries untouched",
                       time.monotonic() - started)


# ---- prompt #8 — web_search fallback chain ---------------------------------

def check_08_web_fallback(live: bool) -> CheckResult:
    started = time.monotonic()
    from unittest.mock import patch
    from coding_agent.tools.web import _relax_query, _web_search

    if _relax_query('"latest" iPhone with USB-C') != "latest iPhone USB-C":
        return CheckResult("08 web_search fallback", "FAIL",
                           "_relax_query did not drop quotes / filler",
                           time.monotonic() - started)

    # Zero-hit branch (mock DDG to return empty first time, then non-empty)
    calls: list[str] = []

    def fake_ddg(q: str):
        calls.append(q)
        if len(calls) == 1:
            return []
        return [{"title": "Found", "url": "https://x.example.com"}]

    os.environ.pop("BRAVE_API_KEY", None)
    with patch("coding_agent.tools.web._duckduckgo_search", side_effect=fake_ddg):
        result = json.loads(_web_search({"query": "the latest news today"}))
    if "duckduckgo_relaxed" not in result["_meta"]["backends_tried"]:
        return CheckResult("08 web_search fallback", "FAIL",
                           f"relaxed retry didn't fire: {result['_meta']}",
                           time.monotonic() - started)
    if not result["results"]:
        return CheckResult("08 web_search fallback", "FAIL",
                           "no results returned after relaxed retry",
                           time.monotonic() - started)

    return CheckResult("08 web_search fallback", "PASS",
                       f"zero-hit → relaxed retry recovered ({calls[1]!r})",
                       time.monotonic() - started)


# ---- prompt #9 — Coordinator subagent memory access ------------------------

def check_09_coordinator_memory(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.core.coordinator import ROLE_TOOLS, WorkerRole

    research = ROLE_TOOLS[WorkerRole.RESEARCH]
    missing = [t for t in ("memory_list", "memory_read", "memory_search") if t not in research]
    if missing:
        return CheckResult("09 coordinator memory access", "FAIL",
                           f"RESEARCH workers missing tools: {missing}",
                           time.monotonic() - started)
    if "memory_save" in ROLE_TOOLS[WorkerRole.WORK]:
        return CheckResult("09 coordinator memory access", "FAIL",
                           "WORK workers should not have memory_save",
                           time.monotonic() - started)
    if "memory_list" not in ROLE_TOOLS[WorkerRole.WORK]:
        return CheckResult("09 coordinator memory access", "FAIL",
                           "WORK workers missing memory_list (read-only)",
                           time.monotonic() - started)

    return CheckResult("09 coordinator memory access", "PASS",
                       "RESEARCH has list/read/search; WORK has list/read only",
                       time.monotonic() - started)


# ---- prompt #10 — Negative regression (plain read_file) --------------------

def check_10_negative_regression(live: bool) -> CheckResult:
    started = time.monotonic()
    from coding_agent.config.settings import AppConfig
    from coding_agent.tools import ToolRegistry
    from coding_agent.tools.filesystem import _read_file

    with tempfile.TemporaryDirectory() as tmp:
        ws = Path(tmp)
        reg = ToolRegistry(workspace_root=ws, config=AppConfig())
        (ws / "README.md").write_text("# Project\n\nhello world\n", encoding="utf-8")
        try:
            result = _read_file(reg, {"path": "README.md"})
        except Exception as exc:
            return CheckResult("10 negative regression", "FAIL",
                               f"text file read raised: {exc}",
                               time.monotonic() - started)
        if "hello world" not in result:
            return CheckResult("10 negative regression", "FAIL",
                               f"content missing from result: {result[:200]}",
                               time.monotonic() - started)
        # AGENTS.md should also read as text
        (ws / "AGENTS.md").write_text("# Project\n\nrules\n", encoding="utf-8")
        try:
            agents = _read_file(reg, {"path": "AGENTS.md"})
        except Exception as exc:
            return CheckResult("10 negative regression", "FAIL",
                               f"AGENTS.md raised: {exc}",
                               time.monotonic() - started)
        if "rules" not in agents:
            return CheckResult("10 negative regression", "FAIL",
                               "AGENTS.md not read as text",
                               time.monotonic() - started)

    return CheckResult("10 negative regression", "PASS",
                       "README.md + AGENTS.md read as plain text (no office routing)",
                       time.monotonic() - started)


# ---- live runner -----------------------------------------------------------

def _run_live_prompt(prompt: str, workspace: Path | None = None) -> dict:
    """Send *prompt* through the configured agent. Returns a dict with
    final_text, tool_names, error."""
    from coding_agent.config import load_app_config
    from coding_agent.core.runtime import AgentRuntime

    workspace = workspace or REPO_ROOT
    try:
        config = load_app_config(None)
        runtime = AgentRuntime(workspace, config)
        tool_names: list[str] = []

        def on_event(ev: dict) -> None:
            if ev.get("type") == "tool_call":
                tool_names.append(ev.get("name", "?"))

        summary = runtime.orchestrate(prompt, event_callback=on_event)
        return {
            "final_text": summary.final_text,
            "tool_names": tool_names,
            "error": "",
        }
    except Exception as exc:
        return {"final_text": "", "tool_names": [], "error": f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"}


# ---- report writer ---------------------------------------------------------

def write_report(results: list[CheckResult], out_path: Path, *, live: bool) -> None:
    counts: dict[str, int] = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1

    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"yucode v0.6.0 smoke test report")
    lines.append(f"generated: {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"mode:      {'LIVE (real provider calls)' if live else 'OFFLINE (code paths only)'}")
    lines.append(f"summary:   {counts['PASS']} pass, {counts['FAIL']} fail, {counts['SKIP']} skip")
    lines.append("=" * 72)
    lines.append("")

    for r in results:
        marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}.get(r.status, "?")
        lines.append(f"[{marker} {r.status}] {r.name}  ({r.duration_s*1000:.0f}ms)")
        if r.detail:
            for sub in r.detail.split("\n"):
                lines.append(f"    {sub}")
        for ev in r.events:
            lines.append(f"    · {ev}")
        lines.append("")

    if counts["FAIL"]:
        lines.append(f">>> {counts['FAIL']} failure(s) — re-run with --live for end-to-end "
                     "verification of the items marked offline-only.")
    else:
        lines.append(">>> all assertions passed.")

    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---- main ------------------------------------------------------------------

CHECKS = [
    check_01_time_sensitive,
    check_02_office_routing,
    check_03_memory_roundtrip,
    check_04_slash_commands,
    check_05_init_agents,
    check_06_init_memory,
    check_07_staleness,
    check_08_web_fallback,
    check_09_coordinator_memory,
    check_10_negative_regression,
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="Also run the LLM-dependent checks through the configured provider.")
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.txt")),
                        help="Output report path (default: tests/results.txt).")
    parser.add_argument("--only", type=int, nargs="*", default=None,
                        help="Run only specific check numbers (1..10). Default: run all.")
    args = parser.parse_args(argv)

    results: list[CheckResult] = []
    for i, check in enumerate(CHECKS, start=1):
        if args.only and i not in args.only:
            continue
        try:
            r = check(args.live)
        except Exception as exc:
            r = CheckResult(check.__name__, "FAIL",
                            f"unexpected exception: {exc}\n{traceback.format_exc()}")
        results.append(r)
        marker = {"PASS": "✓", "FAIL": "✗", "SKIP": "—"}.get(r.status, "?")
        print(f"  [{marker}] {r.name}", file=sys.stderr)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(results, out, live=args.live)
    print(f"\nReport written to {out}", file=sys.stderr)

    return 1 if any(r.status == "FAIL" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
