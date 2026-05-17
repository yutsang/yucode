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
import time
import traceback
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))


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


def run_prompt(prompt: str, workspace: Path) -> dict:
    """Send *prompt* through the configured agent. Return capture dict."""
    from coding_agent.config import load_app_config
    from coding_agent.core.runtime import AgentRuntime

    started = time.monotonic()
    tool_calls: list[dict] = []
    error = ""
    final_text = ""

    try:
        config = load_app_config(None)
        runtime = AgentRuntime(workspace, config)

        def on_event(ev: dict) -> None:
            if ev.get("type") == "tool_call":
                tool_calls.append({
                    "name": ev.get("name", "?"),
                    "args": (ev.get("arguments", "") or "")[:200],
                })

        summary = runtime.orchestrate(prompt, event_callback=on_event)
        final_text = summary.final_text or ""
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()}"

    return {
        "prompt": prompt,
        "final_text": final_text,
        "tool_calls": tool_calls,
        "tool_names": [tc["name"] for tc in tool_calls],
        "duration_s": time.monotonic() - started,
        "error": error,
    }


def write_report(results: list[dict], out_path: Path, workspace: Path) -> None:
    lines: list[str] = []
    lines.append("=" * 78)
    lines.append("yucode v0.6.0 — 10-prompt LIVE run")
    lines.append(f"generated:  {datetime.now().isoformat(timespec='seconds')}")
    lines.append(f"workspace:  {workspace}")
    lines.append(f"prompts:    {len(results)}")
    err_count = sum(1 for r in results if r["error"])
    lines.append(f"errors:     {err_count}")
    total = sum(r["duration_s"] for r in results)
    lines.append(f"total time: {total:.1f}s")
    lines.append("=" * 78)
    lines.append("")

    for i, r in enumerate(results, start=1):
        lines.append(f"--- [{i:02d}] {'ERROR' if r['error'] else 'OK'}  ({r['duration_s']:.1f}s)")
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
            lines.append("ERROR:")
            for ln in r["error"].splitlines():
                lines.append(f"  {ln}")
        else:
            lines.append("ANSWER:")
            answer = r["final_text"].strip() or "(empty)"
            for ln in answer.splitlines():
                lines.append(f"  {ln}")
        lines.append("")

    out_path.write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=str(Path(__file__).with_name("results.txt")),
                        help="Report path (default: tests/results.txt).")
    parser.add_argument("--workspace", default=str(REPO_ROOT),
                        help="Workspace dir to run prompts from (default: repo root).")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).resolve()
    out = Path(args.out).resolve()

    print(f"Workspace: {workspace}", file=sys.stderr)
    print(f"Report:    {out}", file=sys.stderr)
    print(f"Prompts:   {len(PROMPTS)}", file=sys.stderr)
    print("", file=sys.stderr)

    results: list[dict] = []
    for i, prompt in enumerate(PROMPTS, start=1):
        short = prompt[:60].replace("\n", " ") + ("…" if len(prompt) > 60 else "")
        print(f"  [{i:02d}/{len(PROMPTS)}] {short}", file=sys.stderr)
        result = run_prompt(prompt, workspace)
        results.append(result)
        marker = "✗" if result["error"] else "✓"
        tools = ", ".join(result["tool_names"][:5]) or "(no tools)"
        print(f"          {marker} {result['duration_s']:.1f}s · {tools}", file=sys.stderr)

    out.parent.mkdir(parents=True, exist_ok=True)
    write_report(results, out, workspace)
    print(f"\n✓ Report written to {out}", file=sys.stderr)

    return 1 if any(r["error"] for r in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
