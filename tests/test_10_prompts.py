"""Auto-run 10 prompts through the configured yucode agent and write a report.

Usage:
    python tests/test_10_prompts.py
    python tests/test_10_prompts.py --out report.txt

Reads your default yucode config (provider + model + API key). Each prompt is
sent through the full agent runtime, the tool calls and final text are
captured, and a plain-text report is written to results.txt (or --out).

No assertions, no flags, no setup — just send and record.
"""

from __future__ import annotations

import argparse
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

DEFAULT_TIMEOUT_S = 120  # per-prompt watchdog; cancel the agent if it runs longer


# The 10 test prompts. Edit this list to change what runs.
PROMPTS: list[str] = [
    # 1. Time-sensitive grounding (the Dragonair fix)
    "香港站市區預辦登機現在有哪些航空公司？",

    # 2. Office / PDF routing — agent should use inspect_excel_sheets, not bash
    "Read any .xlsx file in the current directory and tell me what's in it. "
    "If there is no .xlsx file, say so.",

    # 3. Persistent memory — list saved memories
    "Use the memory_list tool to show me all my saved memories, then summarise.",

    # 4. Workspace search — plain text file, should use read_file (not office tool)
    "Read README.md in the workspace and summarise it in two sentences.",

    # 5. Web fallback — fictitious package, should not fabricate a version
    "What is the npm package version of 'fooglefnordium-quux-7'?",

    # 6. Time-sensitive in Chinese
    "今年世界盃冠軍是哪支球隊？",

    # 7. Latest software version — time-sensitive
    "What is the latest stable Python version and when was it released?",

    # 8. Workspace exploration — should use list_directory / glob_search
    "List the top-level files and directories in the current workspace.",

    # 9. Instruction file recognition
    "Is there an AGENTS.md or CLAUDE.md or YUCODE.md in this workspace? "
    "If yes, read it and tell me the key conventions.",

    # 10. Grep / search across files
    "Search the workspace for the string 'memory_save' and tell me which files "
    "reference it and how many times.",
]


def run_prompt(prompt: str, workspace: Path, timeout_s: int) -> dict:
    """Send *prompt* through the configured agent. Return capture dict.

    A watchdog timer cancels the agent at *timeout_s* seconds so a hung
    provider call doesn't freeze the whole 10-prompt run. Ctrl+C also
    cancels the current prompt (caller catches KeyboardInterrupt and
    moves to the next one).
    """
    from coding_agent.config import load_app_config
    from coding_agent.core.runtime import AgentRuntime

    started = time.monotonic()
    tool_calls: list[dict] = []
    error = ""
    final_text = ""
    status = "OK"
    runtime_ref: dict[str, object] = {}

    def watchdog() -> None:
        rt = runtime_ref.get("rt")
        if rt is not None:
            try:
                rt.cancel()  # type: ignore[attr-defined]
            except Exception:
                pass

    timer = threading.Timer(timeout_s, watchdog)
    timer.daemon = True

    try:
        config = load_app_config(None)
        runtime = AgentRuntime(workspace, config)
        runtime_ref["rt"] = runtime

        def on_event(ev: dict) -> None:
            if ev.get("type") == "tool_call":
                tool_calls.append({
                    "name": ev.get("name", "?"),
                    "args": (ev.get("arguments", "") or "")[:200],
                })

        timer.start()
        summary = runtime.orchestrate(prompt, event_callback=on_event)
        final_text = summary.final_text or ""
        elapsed = time.monotonic() - started
        if elapsed >= timeout_s and not final_text:
            status = "TIMEOUT"
            error = f"prompt exceeded {timeout_s}s watchdog and was cancelled"
    except KeyboardInterrupt:
        status = "INTERRUPTED"
        error = "Ctrl+C pressed — skipped to next prompt"
        try:
            if "rt" in runtime_ref:
                runtime_ref["rt"].cancel()  # type: ignore[attr-defined]
        except Exception:
            pass
    except Exception as exc:
        status = "ERROR"
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    finally:
        timer.cancel()

    return {
        "prompt": prompt,
        "final_text": final_text,
        "tool_calls": tool_calls,
        "tool_names": [tc["name"] for tc in tool_calls],
        "duration_s": time.monotonic() - started,
        "error": error,
        "status": status,
    }


def write_report(results: list[dict], out_path: Path, workspace: Path, timeout_s: int) -> None:
    counts = {"OK": 0, "TIMEOUT": 0, "INTERRUPTED": 0, "ERROR": 0}
    for r in results:
        counts[r.get("status", "ERROR")] = counts.get(r.get("status", "ERROR"), 0) + 1

    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("yucode v0.6.0 — 10-prompt LIVE run")
    lines.append(f"generated:  {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"workspace:  {workspace}")
    lines.append(f"timeout:    {timeout_s}s per prompt")
    lines.append(f"prompts:    {len(results)}")
    lines.append(f"summary:    {counts['OK']} ok · {counts['TIMEOUT']} timeout · "
                 f"{counts['INTERRUPTED']} interrupted · {counts['ERROR']} error")
    total = sum(r["duration_s"] for r in results)
    lines.append(f"total time: {total:.1f}s")
    lines.append("=" * 78)
    lines.append("")

    for i, r in enumerate(results, start=1):
        status = r.get("status", "ERROR")
        lines.append(f"--- [{i:02d}] {status}  ({r['duration_s']:.1f}s)")
        lines.append(f"PROMPT:")
        for ln in r["prompt"].splitlines():
            lines.append(f"  {ln}")
        lines.append("")
        if r["tool_calls"]:
            lines.append(f"TOOLS ({len(r['tool_calls'])}):")
            for tc in r["tool_calls"]:
                lines.append(f"  • {tc['name']}  {tc['args']}")
        else:
            lines.append("TOOLS: (none called)")
        lines.append("")
        if r["error"]:
            lines.append(f"{status} DETAIL:")
            for ln in r["error"].splitlines():
                lines.append(f"  {ln}")
        if r["final_text"]:
            lines.append("ANSWER:")
            for ln in r["final_text"].strip().splitlines():
                lines.append(f"  {ln}")
        elif not r["error"]:
            lines.append("ANSWER: (empty)")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.txt")),
                        help="Report path (default: tests/results.txt).")
    parser.add_argument("--workspace", default=str(REPO_ROOT),
                        help="Workspace dir to run prompts from (default: repo root).")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S,
                        help=f"Per-prompt timeout in seconds (default: {DEFAULT_TIMEOUT_S}). "
                             "If exceeded, the agent is cancelled and the runner moves to "
                             "the next prompt.")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    out = Path(args.out).resolve()

    print(f"Workspace: {workspace}", file=sys.stderr)
    print(f"Report:    {out}", file=sys.stderr)
    print(f"Prompts:   {len(PROMPTS)}", file=sys.stderr)
    print(f"Timeout:   {args.timeout}s per prompt", file=sys.stderr)
    print("(Ctrl+C skips the current prompt; Ctrl+C twice quickly exits.)", file=sys.stderr)
    print("", file=sys.stderr)

    results: list[dict] = []
    last_interrupt: float = 0.0
    for i, prompt in enumerate(PROMPTS, start=1):
        short = prompt[:60].replace("\n", " ") + ("…" if len(prompt) > 60 else "")
        print(f"  [{i:02d}/{len(PROMPTS)}] {short}", file=sys.stderr)
        result = run_prompt(prompt, workspace, args.timeout)
        results.append(result)
        marker = {"OK": "✓", "TIMEOUT": "⏱", "INTERRUPTED": "⌁", "ERROR": "✗"}.get(
            result.get("status", "ERROR"), "?"
        )
        tools = ", ".join(result["tool_names"][:5]) or "(no tools)"
        print(f"          {marker} {result['status']}  {result['duration_s']:.1f}s · {tools}",
              file=sys.stderr)
        # Double Ctrl+C inside ~2s exits the whole run early
        if result.get("status") == "INTERRUPTED":
            now = time.monotonic()
            if now - last_interrupt < 2.0:
                print("\n  Second Ctrl+C — exiting and writing partial report.", file=sys.stderr)
                break
            last_interrupt = now

    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(results, out, workspace, args.timeout)
    print(f"\n✓ Report written to {out}", file=sys.stderr)

    return 1 if any(r.get("status") != "OK" for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
