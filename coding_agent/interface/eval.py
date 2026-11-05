"""Evaluation runner: capture tool calls / reads / final text for a prompt suite.

Designed to run on a target machine (e.g. a Qwen3-32B box) and emit a single
JSON file that can be shipped back for offline comparison.

Each suite entry may carry an `expected:` block; the mechanically checkable
parts of it are graded here into per-criterion verdicts (same shape as
diagnose_agent.py) and a non-zero exit code, so a regression is visible without
reading 12 blobs of JSON by hand. The free-text `acceptable_conclusions` /
`unacceptable_conclusions` are still for a human — see `_judge`.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ..config import load_app_config
from ..config.settings import resolve_intelligence_tier
from ..core.coordinator import is_complex_prompt
from ..core.runtime import AgentRuntime
from .render import error as render_error

_ARTIFACT_TOKENS = (".pyc", ".pyo", "__pycache__", "node_modules", ".egg-info")


@dataclass
class Verdict:
    """One machine-checkable criterion. Same shape as diagnose_agent.py's
    Verdict so both reports read (and serialize) alike."""
    criterion: str
    passed: bool
    detail: str = ""


@dataclass
class PromptResult:
    id: str
    category: str
    prompt: str
    iterations: int
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)
    artifact_mentions: list[str] = field(default_factory=list)
    final_text: str = ""
    used_coordinator: bool = False
    error: str | None = None
    expected: dict[str, Any] | None = None
    verdicts: list[Verdict] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "category": self.category,
            "prompt": self.prompt,
            "iterations": self.iterations,
            "tool_call_count": len(self.tool_calls),
            "tool_calls": self.tool_calls,
            "files_read": self.files_read,
            "files_read_count": len(self.files_read),
            "artifact_mentions": self.artifact_mentions,
            "final_text": self.final_text,
            "used_coordinator": self.used_coordinator,
            "error": self.error,
            "expected": self.expected,
            "verdicts": [
                {"criterion": v.criterion, "passed": v.passed, "detail": v.detail}
                for v in self.verdicts
            ],
            # None (not True) when the entry carried no gradable criteria —
            # "nothing to check" must not read as "checked and fine".
            "passed": all(v.passed for v in self.verdicts) if self.verdicts else None,
            "needs_human_review": _needs_human_review(self.expected),
        }


def _needs_human_review(expected: dict[str, Any] | None) -> bool:
    """True when the entry lists conclusions no check here can judge."""
    if not expected:
        return False
    return bool(expected.get("acceptable_conclusions")
                or expected.get("unacceptable_conclusions"))


def _judge(result: PromptResult) -> list[Verdict]:
    """Grade one run against its suite entry's `expected:` block.

    Only the mechanically checkable fields become verdicts:

      max_tool_calls             upper bound on tool calls for this prompt —
                                 an efficiency budget, not a hard runtime cap
      must_read_files            true  = the run must open at least one file
                                 false = reading is optional, NOT forbidden
                                 (see the yaml comments on B2/D1: "acceptable
                                 to stop after grep returns nothing")
      must_not_mention_artifacts the final answer must not cite build
                                 artifacts (.pyc / __pycache__ / node_modules
                                 / .egg-info)

    `acceptable_conclusions` / `unacceptable_conclusions` are prose written for
    a human reader ("names settings.py + runtime.py + cites specific lines") —
    matching them by keyword would manufacture false verdicts, so they are
    echoed into the JSON under `expected` and flagged via `needs_human_review`
    instead. Their concrete parts ("surfaces .pyc paths", "guesses without
    reading any source file") are already covered by the two flags above.
    """
    if result.error:
        return [Verdict("run_completed", False, result.error)]
    expected = result.expected
    if not expected:
        return []
    verdicts: list[Verdict] = []
    cap = expected.get("max_tool_calls")
    if isinstance(cap, int) and not isinstance(cap, bool):
        count = len(result.tool_calls)
        verdicts.append(Verdict(
            "tool_calls_within_budget", count <= cap, f"{count} tool calls (cap {cap})",
        ))
    if expected.get("must_read_files") is True:
        verdicts.append(Verdict(
            "read_at_least_one_file", bool(result.files_read),
            f"{len(result.files_read)} file(s) read",
        ))
    if expected.get("must_not_mention_artifacts") is True:
        mentions = result.artifact_mentions
        verdicts.append(Verdict(
            "no_build_artifacts_in_answer", not mentions,
            ", ".join(mentions) if mentions else "",
        ))
    return verdicts


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="yucode eval")
    parser.add_argument("--suite", required=True, help="Path to eval_prompts.yaml.")
    parser.add_argument("--out", required=True, help="Output JSON file path.")
    parser.add_argument("--workspace", default=".", help="Workspace to evaluate against.")
    parser.add_argument("--config-path", default=None, dest="config_path")
    parser.add_argument(
        "--only",
        default=None,
        help="Comma-separated prompt IDs to run (default: all).",
    )
    parser.add_argument(
        "--label",
        default="",
        help="Free-form label written into the output (e.g. 'before-fix' / 'after-fix').",
    )
    return parser.parse_args(argv)


def _load_suite(path: Path) -> list[dict[str, Any]]:
    from ..config.simple_yaml import load_yaml
    raw = load_yaml(path.read_text(encoding="utf-8"))
    prompts = raw.get("prompts") if isinstance(raw, dict) else None
    if not isinstance(prompts, list):
        raise SystemExit(f"Suite {path} is missing a 'prompts:' list.")
    return prompts


def _git_sha(workspace: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _capture_callback(captured: list[dict[str, Any]]):
    def _cb(event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind in {"tool_call", "tool_result", "phase_started", "worker_spawned",
                    "validation_result", "dedup_limit", "stuck_exit"}:
            captured.append(event)
    return _cb


def _extract_files_read(events: list[dict[str, Any]]) -> list[str]:
    files: list[str] = []
    for event in events:
        if event.get("type") != "tool_call":
            continue
        if event.get("name") != "read_file":
            continue
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            continue
        path = arguments.get("path")
        if isinstance(path, str) and path:
            files.append(path)
    return files


def _extract_tool_summary(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") != "tool_call":
            continue
        name = event.get("name", "")
        # Keep arguments compact — full content can be enormous.
        try:
            arguments = json.loads(event.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {"_raw": (event.get("arguments") or "")[:200]}
        # Truncate long string args to keep the JSON readable
        compact: dict[str, Any] = {}
        for k, v in arguments.items():
            if isinstance(v, str) and len(v) > 200:
                compact[k] = v[:200] + "…"
            else:
                compact[k] = v
        summary.append({"name": name, "args": compact})
    return summary


def _find_artifact_mentions(text: str) -> list[str]:
    found: list[str] = []
    lower = text.lower()
    for token in _ARTIFACT_TOKENS:
        if token in lower:
            found.append(token)
    # Also catch explicit file paths ending in .pyc
    matches = re.findall(r"[\w./\\-]+\.pyc\b", text)
    for m in matches:
        if m not in found:
            found.append(m)
    return found


_EVAL_RETRY_WAIT_SECONDS = 30


def _run_one(
    entry: dict[str, Any],
    workspace: Path,
    config_path: str | None,
) -> PromptResult:
    import time as _time

    prompt_id = str(entry.get("id", "?"))
    category = str(entry.get("category", "uncategorized"))
    prompt_text = str(entry.get("prompt", "")).strip()
    expected = entry.get("expected")

    result = PromptResult(
        id=prompt_id,
        category=category,
        prompt=prompt_text,
        iterations=0,
        expected=expected if isinstance(expected, dict) else None,
    )
    if not prompt_text:
        result.error = "empty prompt"
        return result

    config = load_app_config(config_path, workspace=workspace)
    tier = config.provider.resolved_tier()
    result.used_coordinator = (
        config.runtime.orchestration_mode == "multi"
        or (
            config.runtime.orchestration_mode == "auto"
            and is_complex_prompt(prompt_text, intelligence_tier=tier)
        )
    )

    # Outer retry — when the gateway is having a sustained outage, the
    # provider's in-call retries (5× with ~46s total backoff) can still fail.
    # Wait longer and try the whole prompt again once.
    last_exc: Exception | None = None
    for attempt in (1, 2):
        runtime = AgentRuntime(workspace, config)
        events: list[dict[str, Any]] = []
        try:
            summary = runtime.orchestrate(prompt_text, event_callback=_capture_callback(events))
            last_exc = None
            break
        except Exception as exc:  # noqa: BLE001 — eval must keep going across prompts
            last_exc = exc
            is_transient = "502" in str(exc) or "503" in str(exc) or "504" in str(exc) or "timeout" in str(exc).lower()
            if attempt == 1 and is_transient:
                print(f"      ↳ transient error, retrying in {_EVAL_RETRY_WAIT_SECONDS}s ({type(exc).__name__})")
                _time.sleep(_EVAL_RETRY_WAIT_SECONDS)
                continue
            break

    if last_exc is not None:
        result.error = f"{type(last_exc).__name__}: {last_exc}"
        return result

    result.iterations = summary.iterations
    result.final_text = summary.final_text
    result.tool_calls = _extract_tool_summary(events)
    result.files_read = _extract_files_read(events)
    result.artifact_mentions = _find_artifact_mentions(summary.final_text)
    return result


def run(argv: list[str] | None = None) -> int:
    args = _parse_args(argv if argv is not None else sys.argv[1:])
    suite_path = Path(args.suite).expanduser().resolve()
    out_path = Path(args.out).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()

    if not suite_path.is_file():
        print(render_error(f"Suite not found: {suite_path}"), file=sys.stderr)
        return 1

    only_ids = {s.strip() for s in args.only.split(",") if s.strip()} if args.only else None

    entries = _load_suite(suite_path)
    if only_ids:
        entries = [e for e in entries if str(e.get("id", "")) in only_ids]
    if not entries:
        print(render_error("No prompts to run."), file=sys.stderr)
        return 1

    # Snapshot resolved tier for the report header.
    config = load_app_config(args.config_path, workspace=workspace)
    tier_resolved = resolve_intelligence_tier(config.provider.intelligence_tier, config.provider.model)

    results: list[PromptResult] = []
    print(f"Running {len(entries)} prompt(s) against {workspace}")
    print(f"  model = {config.provider.model!r}  tier = {tier_resolved}  label = {args.label!r}\n")

    for i, entry in enumerate(entries, 1):
        prompt_id = entry.get("id", "?")
        print(f"  [{i}/{len(entries)}] {prompt_id} ...", flush=True)
        result = _run_one(entry, workspace, args.config_path)
        result.verdicts = _judge(result)
        results.append(result)
        marker = "ERROR" if result.error else (
            f"{result.iterations}it / {len(result.tool_calls)} calls / "
            f"{len(result.files_read)} reads"
            + (" / .pyc!" if result.artifact_mentions else "")
        )
        print(f"      → {marker}")
        for verdict in result.verdicts:
            mark = "PASS" if verdict.passed else "FAIL"
            detail = f": {verdict.detail}" if verdict.detail else ""
            print(f"        [{mark}] {verdict.criterion}{detail}")

    verdicts_total = sum(len(r.verdicts) for r in results)
    verdicts_passed = sum(1 for r in results for v in r.verdicts if v.passed)
    failed_ids = [r.id for r in results if any(not v.passed for v in r.verdicts)]
    review_ids = [r.id for r in results if _needs_human_review(r.expected)]

    payload = {
        "version": 1,
        "label": args.label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "workspace": str(workspace),
        "git_sha": _git_sha(workspace),
        "model": config.provider.model,
        "intelligence_tier_config": config.provider.intelligence_tier,
        "intelligence_tier_resolved": tier_resolved,
        "orchestration_mode": config.runtime.orchestration_mode,
        "max_iterations": config.runtime.max_iterations,
        "max_coordinator_retries": config.runtime.max_coordinator_retries,
        "max_tool_calls": config.runtime.max_tool_calls,
        "suite": str(suite_path),
        "verdicts_total": verdicts_total,
        "verdicts_passed": verdicts_passed,
        "prompts_failed": failed_ids,
        "prompts_needing_human_review": review_ids,
        "results": [r.to_dict() for r in results],
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {out_path}")

    coordinator_count = sum(1 for r in results if r.used_coordinator)
    artifact_count = sum(1 for r in results if r.artifact_mentions)
    error_count = sum(1 for r in results if r.error)
    print(f"  used_coordinator = {coordinator_count}/{len(results)}")
    print(f"  artifact_mentions = {artifact_count}/{len(results)}")
    print(f"  errors = {error_count}/{len(results)}")
    print(f"  verdicts = {verdicts_passed}/{verdicts_total} passed")
    if failed_ids:
        print(f"  FAILED = {', '.join(failed_ids)}")
    if review_ids:
        print("  needs human review (acceptable/unacceptable_conclusions) = "
              + ", ".join(review_ids))
    # Non-zero when any hard criterion failed — a run that errored gets a
    # failing `run_completed` verdict, so this covers errors too.
    return 1 if failed_ids else 0


if __name__ == "__main__":
    raise SystemExit(run())
