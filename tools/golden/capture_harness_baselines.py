#!/usr/bin/env python3
"""Freeze the Python harness's observable behaviour as comparison baselines.

Run this while the Python tree still exists. Everything it writes under
`tests/golden/harness/` is an oracle for a work package in the TypeScript
rewrite (see docs/REWRITE_PLAN.md):

    config_control_dict.*.json   WP1   config discovery, merge and defaults
    tool_definitions.json        WP4   every tool's name, schema and permission
    system_prompt.*.json         WP7   prompt section order and content
    provider_messages.3turn.json WP3   session -> provider message projection
    state_layout.json            WP1   workspace key hashing and directory names

The two baselines that need a live model (`yucode eval`, `diagnose_agent.py`)
are captured by running those programs directly; see docs/REWRITE_PLAN.md §4.

Offline output is byte-stable: run it twice and `git status` stays clean.
That property is the point -- a baseline that churns cannot tell you whether
the TypeScript port drifted or the capture did.

Usage:
    python tools/golden/capture_harness_baselines.py [--out DIR] [--check]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO_ROOT / "tests" / "golden" / "harness"

# The capture must not read or write the operator's real ~/.yucode, and
# settings.py snapshots YUCODE_HOME into a module constant at import time
# (settings.py:58), so this has to happen before the coding_agent imports.
_SCRATCH_HOME = Path(tempfile.mkdtemp(prefix="yucode-baseline-home-"))
os.environ["YUCODE_HOME"] = str(_SCRATCH_HOME)
# An exported key would beat the config file (settings.py:81) and make the
# captured control dicts depend on the operator's shell.
os.environ.pop("YUCODE_API_KEY", None)

sys.path.insert(0, str(REPO_ROOT))

from coding_agent.config import AppConfig, ProviderConfig, RuntimeOptions  # noqa: E402
from coding_agent.config.settings import (  # noqa: E402
    BUNDLED_CONFIG_PATH,
    load_app_config,
    state_dir,
    workspace_key,
)
from coding_agent.core.session import Message, Session, ToolCall  # noqa: E402
from coding_agent.memory.prompting import (  # noqa: E402
    PromptAssembler,
    discover_project_context,
)
from coding_agent.tools import ToolRegistry  # noqa: E402

BASELINE_DATE = "2026-09-12"

# Tokens replace machine-specific absolute paths so a baseline captured on one
# machine can be compared against a run on another.
WS_TOKEN = "<WS>"
HOME_TOKEN = "<YUCODE_HOME>"
REPO_TOKEN = "<REPO>"


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    path.write_text(text + "\n", encoding="utf-8")


def scrub(text: str, replacements: list[tuple[str, str]]) -> str:
    """Replace absolute paths with tokens, longest prefix first.

    Longest-first matters because the scratch home lives inside the system
    temp dir; replacing the shorter prefix first would leave a half-tokenised
    path behind.
    """
    for needle, token in sorted(replacements, key=lambda pair: -len(pair[0])):
        text = text.replace(needle, token)
    return text


def mask_api_key(control: dict[str, Any]) -> dict[str, Any]:
    """Replace the key with its length so presence is comparable, value is not."""
    key = control.get("provider", {}).get("api_key") or ""
    control["provider"]["api_key"] = f"<masked len={len(key)}>" if key else ""
    return control


# --------------------------------------------------------------------------
# fixture workspace
# --------------------------------------------------------------------------


def build_fixture_workspace(root: Path, *, as_git_repo: bool) -> Path:
    """Create the workspace the prompt baselines are rendered against.

    Fixed content, so the rendered prompt is a function of the harness alone.
    The git variant carries one committed file and one staged edit, which is
    what exercises the git status/diff sections of the prompt.
    """
    ws = root / ("ws-git" if as_git_repo else "ws-plain")
    ws.mkdir(parents=True)

    (ws / "AGENTS.md").write_text(
        "# Fixture project\n"
        "\n"
        "## Conventions\n"
        "- Tabs are four spaces.\n"
        "- Every public function carries a docstring.\n",
        encoding="utf-8",
    )
    (ws / "README.md").write_text("# fixture\n\nA workspace used by the baselines.\n", encoding="utf-8")

    skill_dir = ws / ".claw" / "skills" / "fixture-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: fixture-skill\n"
        "description: A skill that exists only so the skills section renders.\n"
        "---\n"
        "\n"
        "# Fixture skill\n"
        "\n"
        "Do the fixture thing.\n",
        encoding="utf-8",
    )

    memory_dir = ws / ".yucode" / "memory"
    memory_dir.mkdir(parents=True)
    (memory_dir / "fixture-fact.md").write_text(
        "---\n"
        "name: fixture-fact\n"
        "description: Something the baseline remembers.\n"
        "saved_at: 2026-09-12T00:00:00Z\n"
        "type: project\n"
        "---\n"
        "\n"
        "The fixture workspace is synthetic.\n",
        encoding="utf-8",
    )
    (memory_dir / "MEMORY.md").write_text(
        "# Memory index\n\n- [fixture-fact](fixture-fact.md) — the workspace is synthetic\n",
        encoding="utf-8",
    )

    src = ws / "src"
    src.mkdir()
    (src / "app.py").write_text("def main() -> None:\n    print('fixture')\n", encoding="utf-8")

    if as_git_repo:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Fixture",
            "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
            "GIT_COMMITTER_NAME": "Fixture",
            "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
            "GIT_AUTHOR_DATE": "2026-09-12T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2026-09-12T00:00:00+00:00",
        }
        run = lambda *args: subprocess.run(  # noqa: E731
            ["git", *args], cwd=ws, env=env, check=True, capture_output=True
        )
        run("init", "-q", "-b", "main")
        run("add", "-A")
        run("commit", "-q", "-m", "Seed the fixture workspace")
        # One tracked edit, so status and diff both have something to report.
        (src / "app.py").write_text(
            "def main() -> None:\n    print('fixture edited')\n", encoding="utf-8"
        )
        (ws / "untracked.txt").write_text("not committed\n", encoding="utf-8")
    return ws


# --------------------------------------------------------------------------
# captures
# --------------------------------------------------------------------------


def capture_config_control_dicts(out: Path, fixture_ws: Path) -> None:
    """WP1's oracle: what every config key resolves to, for two config files.

    Goes through load_app_config().to_control_dict() rather than the
    `show-config` subcommand, which has no --output-format flag and only
    echoes the file (cli.py:519-522).
    """
    sources = {
        "bundled": BUNDLED_CONFIG_PATH,
        "workbench": REPO_ROOT / "docs" / "settings.workbench.yml",
    }
    for label, path in sources.items():
        if not path.is_file():
            raise SystemExit(f"config source missing: {path}")
        config = load_app_config(str(path), workspace=fixture_ws)
        control = mask_api_key(config.to_control_dict())
        write_json(
            out / f"config_control_dict.{label}.json",
            {
                "source": str(path.relative_to(REPO_ROOT)),
                "resolved_intelligence_tier": config.provider.resolved_tier(),
                "control_dict": control,
            },
        )

    # The dataclass defaults matter on their own: they are what a TypeScript
    # zod schema has to reproduce for a config file that sets nothing.
    write_json(
        out / "config_control_dict.dataclass_defaults.json",
        {
            "source": "AppConfig() with no file",
            "resolved_intelligence_tier": AppConfig().provider.resolved_tier(),
            "control_dict": mask_api_key(AppConfig().to_control_dict()),
        },
    )


def capture_tool_definitions(out: Path, fixture_ws: Path) -> None:
    """WP4's oracle: the exact JSON Schema the model is shown for every tool."""
    config = AppConfig(runtime=RuntimeOptions(permission_mode="danger-full-access"))
    registry = ToolRegistry(fixture_ws, config)
    definitions = registry.definitions_for_provider()
    permissions = dict(registry.permission_specs())
    risks = {name: registry.risk_level_for(name) for name in permissions}

    write_json(
        out / "tool_definitions.json",
        {
            "note": (
                "Order is registration order, which is the order the provider "
                "sees. definitions_for_provider() does not sort "
                "(tools/__init__.py:484)."
            ),
            "count": len(definitions),
            "extended_tools_enabled": config.tools.extended_tools_enabled,
            "tools": [
                {
                    "name": entry["function"]["name"],
                    "permission": permissions[entry["function"]["name"]],
                    "risk_level": str(risks[entry["function"]["name"]]),
                    "description": entry["function"]["description"],
                    "parameters": entry["function"]["parameters"],
                }
                for entry in definitions
            ],
        },
    )

    # Same registry, extended stubs on. The rewrite drops all 33 of them
    # (REWRITE_PLAN §A.7); this records what is being dropped.
    from coding_agent.config.settings import ToolOptions

    extended = ToolRegistry(
        fixture_ws,
        AppConfig(
            runtime=RuntimeOptions(permission_mode="danger-full-access"),
            tools=ToolOptions(extended_tools_enabled=True),
        ),
    )
    base_names = {entry["function"]["name"] for entry in definitions}
    extra = [
        name
        for name in (e["function"]["name"] for e in extended.definitions_for_provider())
        if name not in base_names
    ]
    write_json(
        out / "tool_definitions.extended_stubs.json",
        {
            "note": "Registered only when tools.extended_tools_enabled; not ported.",
            "count": len(extra),
            "names": extra,
        },
    )


def capture_system_prompts(out: Path, tmp_root: Path) -> None:
    """WP7's oracle: the rendered prompt, section by section."""
    replacements_home = (str(_SCRATCH_HOME), HOME_TOKEN)

    for label, as_git in (("plain", False), ("git", True)):
        ws = build_fixture_workspace(tmp_root, as_git_repo=as_git)
        config = load_app_config(str(BUNDLED_CONFIG_PATH), workspace=ws)
        context = discover_project_context(
            ws,
            current_date=BASELINE_DATE,
            include_git_context=config.runtime.include_git_context,
            explicit_instruction_files=config.instruction_files,
        )
        assembler = PromptAssembler(config, context)
        replacements = [(str(ws.resolve()), WS_TOKEN), (str(ws), WS_TOKEN), replacements_home]
        sections = [scrub(section, replacements) for section in assembler.build_sections()]
        rendered = scrub(assembler.render(), replacements)
        write_json(
            out / f"system_prompt.{label}.json",
            {
                "date": BASELINE_DATE,
                "workspace": WS_TOKEN,
                "git_repo": as_git,
                "section_count": len(sections),
                "sections": sections,
                "rendered": rendered,
                "rendered_sha256": hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            },
        )


def capture_provider_messages(out: Path) -> None:
    """WP3's oracle: how a session projects into the provider's message list.

    Three turns, one of them carrying a tool call whose arguments are
    deliberately malformed -- `_safe_tool_args` (session.py:82) rewrites those
    to "{}" on the way out, and a port that forwards them verbatim gets the
    whole request rejected.
    """
    session = Session(model="deepseek-chat")
    session.add_message(Message(role="user", content="List the Python files here."))
    session.add_message(
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="glob_search", arguments='{"pattern": "**/*.py"}')],
        )
    )
    session.add_message(
        Message(role="tool", content="src/app.py\nsrc/util.py", tool_call_id="call_1")
    )
    session.add_message(Message(role="assistant", content="Two files: src/app.py and src/util.py."))
    session.add_message(Message(role="user", content="Read the first one."))
    session.add_message(
        Message(
            role="assistant",
            content="",
            # Truncated JSON, exactly the failure _safe_tool_args exists for.
            tool_calls=[ToolCall(id="call_2", name="read_file", arguments='{"path": "src/app.p')],
        )
    )
    session.add_message(
        Message(role="tool", content='{"error": "Invalid JSON in arguments"}', tool_call_id="call_2")
    )
    session.add_message(Message(role="assistant", content="Retrying with a valid path."))
    session.add_message(Message(role="user", content="Summarise what it does."))

    system_prompt = "SYSTEM PROMPT PLACEHOLDER"
    write_json(
        out / "provider_messages.3turn.json",
        {
            "note": (
                "Session.provider_messages(system_prompt, pruning=None). The "
                "stored session.system_prompt is deliberately ignored; the "
                "argument wins (session.py:285-287). call_2's arguments are "
                "malformed on purpose and must project as '{}'."
            ),
            "session_version": session.version,
            "model": session.model,
            "messages_in": [message.to_dict() for message in session.messages],
            "provider_messages": session.provider_messages(system_prompt, pruning=None),
        },
    )


def capture_state_layout(out: Path) -> None:
    """WP1's oracle for the state directory: the hash input and the layout.

    The key is sha256 of `str(Path.resolve())` truncated to 12 hex characters
    (settings.py:61-64). A Windows path is included because that is where a
    port is most likely to hash a different string.
    """
    samples = [
        "/Users/example/Desktop/Github/yucode",
        "/home/example/src/yucode",
        "C:\\Users\\example\\Desktop\\Github\\yucode",
        "/tmp/ws",
    ]
    write_json(
        out / "state_layout.json",
        {
            "note": (
                "workspace_key = sha256(str(Path(p).resolve()))[:12]. These "
                "entries hash the literal string, bypassing resolve(), so the "
                "expectation is machine-independent; the Windows sample is the "
                "one a port is most likely to get wrong."
            ),
            "keys": {
                path: hashlib.sha256(path.encode()).hexdigest()[:12] for path in samples
            },
            "layout": {
                "root": "$YUCODE_HOME|~/.yucode/projects/<workspace_key>",
                "subdirs": [
                    "sessions",
                    "archives",
                    "checkpoints",
                    "audit",
                    "metrics",
                    "tool_output",
                    "bash_output",
                    "exports",
                    "plugins",
                ],
                "files": ["todos.json", "mcp.yml"],
                "legacy_fallback": "<workspace>/.yucode when it exists and the home dir does not",
            },
            "live_check": {
                "workspace": WS_TOKEN,
                "key": workspace_key(Path(tempfile.gettempdir()) / "ws"),
                "state_dir_ends_with": str(
                    state_dir(Path(tempfile.gettempdir()) / "ws")
                ).replace(str(_SCRATCH_HOME), HOME_TOKEN),
            },
        },
    )


def capture_environment(out: Path) -> None:
    """What this capture ran against, so a later diff can be attributed."""
    import platform

    versions: dict[str, str] = {}
    for module in ("openpyxl", "pptx", "docx", "pdfplumber", "PIL", "lxml", "fontTools", "yaml"):
        try:
            versions[module] = __import__(module).__version__
        except Exception as exc:  # noqa: BLE001 - reporting, not control flow
            versions[module] = f"<absent: {type(exc).__name__}>"

    from coding_agent import __version__ as agent_version

    write_json(
        out / "environment.json",
        {
            "yucode_version": agent_version,
            "python": platform.python_version(),
            "platform": platform.platform(),
            "libraries": versions,
            "git_commit": subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip(),
        },
    )


# --------------------------------------------------------------------------
# entry point
# --------------------------------------------------------------------------


def capture_all(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    tmp_root = Path(tempfile.mkdtemp(prefix="yucode-baseline-ws-"))
    try:
        fixture_ws = build_fixture_workspace(tmp_root, as_git_repo=False)
        capture_config_control_dicts(out, fixture_ws)
        capture_tool_definitions(out, fixture_ws)
        shutil.rmtree(fixture_ws)
        capture_system_prompts(out, tmp_root)
        capture_provider_messages(out)
        capture_state_layout(out)
        capture_environment(out)
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output directory")
    parser.add_argument(
        "--check",
        action="store_true",
        help="recapture into a temp dir and diff against --out; exit 1 on drift",
    )
    args = parser.parse_args(argv)

    if not args.check:
        capture_all(args.out)
        print(f"wrote baselines to {args.out}")
        return 0

    scratch = Path(tempfile.mkdtemp(prefix="yucode-baseline-check-"))
    try:
        capture_all(scratch)
        drifted: list[str] = []
        produced = {p.name for p in scratch.glob("*.json")}
        # Only the files this script writes. The diagnostics and the eval suite
        # are captured by running those programs against a live model, so they
        # live in the same directory and are none of this check's business.
        expected = {p.name for p in args.out.glob("*.json")} & produced
        for name in sorted(expected | produced):
            if name not in expected:
                drifted.append(f"  + {name} (new)")
            elif name not in produced:
                drifted.append(f"  - {name} (no longer produced)")
            elif (args.out / name).read_bytes() != (scratch / name).read_bytes():
                # environment.json is expected to move with the machine.
                label = "environment drift" if name == "environment.json" else "BASELINE DRIFT"
                drifted.append(f"  ! {name} ({label})")
        blocking = [line for line in drifted if "environment drift" not in line]
        for line in drifted:
            print(line)
        if blocking:
            print(f"{len(blocking)} file(s) drifted")
            return 1
        print("baselines match")
        return 0
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(_SCRATCH_HOME, ignore_errors=True)
