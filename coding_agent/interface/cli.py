from __future__ import annotations

import argparse
import json
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

from ..config import (
    AppConfig,
    ConfigError,
    McpServerConfig,
    ProviderConfig,
    add_mcp_server_to_config,
    dump_yaml,
    ensure_default_config,
    load_app_config,
    load_yaml,
    remove_mcp_server_from_config,
    resolve_config_path,
)
from ..core.errors import AgentError
from ..core.providers import OpenAICompatibleProvider
from ..core.runtime import AgentRuntime
from ..memory.skills import list_skills
from .bridge import BridgeServer
from .render import (
    BOLD,
    DIM,
    RESET,
    THEME,
    ProgressDisplay,
    agent_label,
    compact_tool_result_line,
    compact_tool_start_label,
    format_assistant_text,
    format_cost_summary,
    format_memory_display,
    section_header,
    startup_banner,
    user_prompt,
)
from .render import (
    error as render_error,
)
from .render import (
    info as render_info,
)
from .render import (
    success as render_success,
)
from .render import (
    warning as render_warning,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="yucode", description="YuCode coding agent CLI.")
    parser.add_argument("--config", dest="config_path")
    # Top-level flags so `yucode` (no subcommand) can accept --workspace etc.
    parser.add_argument("--workspace", default=".", help="Workspace directory (default: current dir).")
    parser.add_argument("--model", default=None, help="Override the chat model.")
    parser.add_argument("--permission-mode", default=None,
                        choices=["read-only", "workspace-write", "danger-full-access", "prompt", "allow"])
    parser.set_defaults(handler=handle_default)
    subparsers = parser.add_subparsers(dest="command", required=False)

    _init_cfg = subparsers.add_parser("init-config", help="Interactive wizard to create/edit all settings in config.yml.")
    _init_cfg.add_argument("--defaults", "-y", action="store_true", help="Write default config without prompting.")
    _init_cfg.set_defaults(handler=handle_init_config)
    subparsers.add_parser("show-config", help="Print the active config.").set_defaults(handler=handle_show_config)
    subparsers.add_parser("config-path", help="Print the active config path.").set_defaults(handler=handle_config_path)
    subparsers.add_parser("bridge", help="Start the JSONL bridge used by VS Code.").set_defaults(handler=handle_bridge)

    init = subparsers.add_parser("init", help="Scaffold a new project directory with config and instruction files.")
    init.add_argument("target", nargs="?", default=".", help="Target directory (default: current dir).")
    init.set_defaults(handler=handle_init)

    chat = subparsers.add_parser("chat", help="Run a coding-agent turn (or start interactive mode with no prompt).")
    chat.add_argument("prompt", nargs="?", default=None)
    chat.add_argument("--workspace", default=".")
    chat.add_argument("--json", action="store_true", dest="json_output")
    chat.add_argument("--model", default=None, help="Override the chat model.")
    chat.add_argument("--allowed-tools", nargs="*", default=None, help="Restrict tools to this list.")
    chat.add_argument("--permission-mode", default=None, choices=["read-only", "workspace-write", "danger-full-access", "prompt", "allow"])
    chat.add_argument("--resume", default=None, metavar="SESSION_ID", help="Resume a saved session by ID.")
    chat.add_argument("--output-format", default="text", choices=["text", "json"], help="Output format.")
    chat.set_defaults(handler=handle_chat)

    mcp_list = subparsers.add_parser("mcp-list", help="List configured MCP servers.")
    mcp_list.add_argument("--workspace", default=".")
    mcp_list.set_defaults(handler=handle_mcp_list)

    mcp_add = subparsers.add_parser("mcp-add", help="Add an MCP server to .yucode/mcp.yml.")
    mcp_add.add_argument("name", help="Unique server name.")
    mcp_add.add_argument("mcp_command", metavar="command", help="Executable command (e.g. uvx, python).")
    mcp_add.add_argument("args", nargs="*", default=[], help="Command arguments.")
    mcp_add.add_argument("--transport", default="stdio", choices=["stdio", "sse", "http", "ws"])
    mcp_add.add_argument("--workspace", default=".")
    mcp_add.set_defaults(handler=handle_mcp_add)

    mcp_remove = subparsers.add_parser("mcp-remove", help="Remove an MCP server from .yucode/mcp.yml.")
    mcp_remove.add_argument("name", help="Server name to remove.")
    mcp_remove.add_argument("--workspace", default=".")
    mcp_remove.set_defaults(handler=handle_mcp_remove)

    mcp_validate = subparsers.add_parser("mcp-validate", help="Validate configured MCP servers can connect.")
    mcp_validate.add_argument("--workspace", default=".")
    mcp_validate.set_defaults(handler=handle_mcp_validate)

    mcp_preset = subparsers.add_parser("mcp-preset", help="Add a pre-built MCP server from the catalog.")
    mcp_preset.add_argument("preset_name", nargs="?", default=None, help="Preset name (omit to list all).")
    mcp_preset.add_argument("preset_args", nargs="*", default=[], help="Extra arguments for the preset.")
    mcp_preset.add_argument("--workspace", default=".")
    mcp_preset.set_defaults(handler=handle_mcp_preset)

    skills = subparsers.add_parser("skills", help="List discovered skill files.")
    skills.add_argument("--workspace", default=".")
    skills.add_argument("--output-format", default="text", choices=["text", "json"])
    skills.set_defaults(handler=handle_skills)

    status = subparsers.add_parser("status", help="Show runtime status summary.")
    status.add_argument("--output-format", default="text", choices=["text", "json"])
    status.set_defaults(handler=handle_status)

    system_prompt = subparsers.add_parser("system-prompt", help="Print the assembled system prompt.")
    system_prompt.add_argument("--workspace", default=".", dest="cwd")
    system_prompt.add_argument("--date", default=None, help="Override the current date (YYYY-MM-DD).")
    system_prompt.add_argument("--output-format", default="text", choices=["text", "json"])
    system_prompt.set_defaults(handler=handle_system_prompt)

    version = subparsers.add_parser("version", help="Show YuCode version.")
    version.add_argument("--output-format", default="text", choices=["text", "json"])
    version.set_defaults(handler=handle_version)

    doctor = subparsers.add_parser("doctor", help="Run preflight diagnostics and health checks.")
    doctor.add_argument("--workspace", default=".")
    doctor.add_argument("--output-format", default="text", choices=["text", "json"])
    doctor.set_defaults(handler=handle_doctor)

    serve = subparsers.add_parser("serve", help="Start the HTTP + SSE session server.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    serve.add_argument("--workspace", default=".")
    serve.set_defaults(handler=handle_serve)

    return parser


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _prompt(label: str, current: Any, *, secret: bool = False, choices: list[str] | None = None) -> Any:
    display = ("*" * min(len(str(current)), 8) + "\u2026") if secret and current else (repr(current) if not isinstance(current, str) else current)
    hint = f" [{', '.join(choices)}]" if choices else ""
    try:
        raw = input(f"  {label}{hint} (current: {display}): ").strip()
    except (EOFError, KeyboardInterrupt):
        raise
    if raw == "":
        return current
    if choices and raw not in choices:
        print(f"    Invalid choice. Keeping: {current}")
        return current
    if isinstance(current, bool):
        return raw.lower() in ("true", "yes", "1", "y")
    if isinstance(current, float):
        try:
            return float(raw)
        except ValueError:
            print(f"    Not a number. Keeping: {current}")
            return current
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            print(f"    Not a number. Keeping: {current}")
            return current
    return raw


_PROJECT_ENV_EXAMPLE = """\
YUCODE_API_KEY=
YUCODE_BASE_URL=
YUCODE_MODEL=
YUCODE_PROVIDER_NAME=
YUCODE_DANGEROUS_MODE=0
"""


def _ensure_project_support_files(project_root: Path) -> list[Path]:
    created: list[Path] = []

    env_example = project_root / ".env.example"
    if not env_example.exists():
        env_example.write_text(_PROJECT_ENV_EXAMPLE, encoding="utf-8")
        created.append(env_example)

    gitignore = project_root / ".gitignore"
    required_entries = [
        ".env",
        ".env.*",
        "!.env.example",
        ".yucode/settings.yml",
        ".yucode/settings.json",
        ".yucode/settings.local.yml",
        ".yucode/settings.local.json",
    ]
    existing_lines: list[str] = []
    if gitignore.exists():
        existing_lines = gitignore.read_text(encoding="utf-8").splitlines()
    missing = [entry for entry in required_entries if entry not in existing_lines]
    if missing:
        prefix = "\n" if existing_lines and existing_lines[-1] != "" else ""
        with gitignore.open("a", encoding="utf-8") as handle:
            handle.write(prefix)
            if not existing_lines or "# Secrets / local config" not in existing_lines:
                handle.write("# Secrets / local config\n")
            for entry in missing:
                handle.write(f"{entry}\n")
        if gitignore not in created:
            created.append(gitignore)

    yucode_dir = project_root / ".yucode"
    yucode_dir.mkdir(parents=True, exist_ok=True)
    local_overlay = yucode_dir / "settings.local.yml"
    if not local_overlay.exists():
        local_overlay.write_text(
            "provider:\n"
            "  api_key: \"\"\n",
            encoding="utf-8",
        )
        created.append(local_overlay)

    return created


def _has_configured_api_key(config_path: str | None, workspace: Path | None = None) -> bool:
    try:
        config = load_app_config(config_path, workspace=workspace)
    except Exception:
        return False
    return bool(config.provider.api_key.strip())


def _test_api_connection(config_path: str | None, workspace: Path | None = None) -> tuple[bool, str]:
    ok, _, message = _probe_provider_connection(config_path, workspace=workspace, stream=False)
    return ok, message


def _probe_provider_connection(
    config_path: str | None,
    workspace: Path | None = None,
    *,
    stream: bool,
) -> tuple[bool, str, str]:
    try:
        config = load_app_config(config_path, workspace=workspace)
    except Exception as exc:  # noqa: BLE001
        return False, "error", f"Could not load config: {exc}"

    if not config.provider.api_key.strip():
        return False, "error", "No API key configured via env or config."
    if not config.provider.base_url.strip():
        return False, "error", "No provider base_url configured."
    if not config.provider.model.strip():
        return False, "error", "No provider model configured."

    test_provider = ProviderConfig(
        name=config.provider.name,
        type=config.provider.type,
        base_url=config.provider.base_url,
        api_key=config.provider.api_key,
        model=config.provider.model,
        chat_path=config.provider.chat_path,
        append_chat_path=config.provider.append_chat_path,
        verify_tls=config.provider.verify_tls,
        stream=stream,
        streaming_mode="stream" if stream else "no_stream",
        temperature=0.0,
        extra_headers=dict(config.provider.extra_headers),
        extra_body=dict(config.provider.extra_body),
    )
    provider = OpenAICompatibleProvider(test_provider)
    effective_url = provider._build_url()  # noqa: SLF001
    mode_name = "Streaming" if stream else "Non-streaming"

    events: list[dict[str, Any]] = []
    try:
        response = provider.complete(
            [{"role": "user", "content": "Reply with exactly OK."}],
            [],
            stream_callback=events.append,
        )
    except Exception as exc:  # noqa: BLE001
        return False, "error", f"{mode_name} request to {effective_url} failed: {exc}"

    text = (response.text or "").strip()
    if text:
        return True, "ok", f"{mode_name} request succeeded: {text[:80]}"
    if response.tool_calls:
        return True, "warning", f"{mode_name} request returned tool calls during smoke test."
    warnings = [e.get("warning", "") for e in events if e.get("type") == "warning"]
    if warnings:
        summary = warnings[0]
        if stream:
            return False, "warning", (
                f"{mode_name} request returned no usable text. {summary} "
                "Try setting `provider.stream: false`."
            )
        return False, "warning", f"{mode_name} request returned no usable text. {summary}"
    if stream:
        return False, "warning", (
            f"{mode_name} request to {effective_url} completed but returned no text or usage. "
            "This provider may not support SSE streaming; try setting `provider.stream: false`."
        )
    return False, "warning", (
        f"{mode_name} request to {effective_url} completed but returned no text."
    )


def handle_init_config(args: argparse.Namespace) -> int:
    path = resolve_config_path(args.config_path)
    config_missing = not path.exists()
    ensure_default_config(path)

    # --defaults / -y: just ensure the file exists with defaults, skip the wizard
    if getattr(args, "defaults", False):
        if config_missing:
            print(f"  Created default config at {path}")
        else:
            print(f"  Config already exists at {path} (unchanged)")
        print("  Edit it directly or re-run without --defaults to use the wizard.")
        return 0

    raw: dict[str, Any] = {}
    with suppress(Exception):
        raw = load_yaml(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raw = {}

    created_support_files: list[Path] = []
    if config_missing and path.parent.name == ".yucode":
        created_support_files = _ensure_project_support_files(path.parent.parent)

    print(f"\n\033[1mYuCode config wizard\033[0m  \u2192  {path}")
    print("Press Enter to keep the current value, or type a new one.\n")
    if created_support_files:
        print("Created support files:")
        for created in created_support_files:
            print(f"  - {created}")
        print()

    try:
        print("\033[36m\u2500\u2500 Provider \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\033[0m")
        p = raw.setdefault("provider", {})
        p["name"]        = _prompt("provider.name        (display name)",   p.get("name", ""))
        p["api_key"]     = _prompt("provider.api_key     (your API key)",   p.get("api_key", ""), secret=True)
        p["base_url"]    = _prompt("provider.base_url    (API endpoint)",   p.get("base_url", ""))
        p["model"]       = _prompt("provider.model       (model name)",     p.get("model", ""))
        p["chat_path"]   = _prompt("provider.chat_path",                    p.get("chat_path", "/chat/completions"))
        p["append_chat_path"] = _prompt(
            "provider.append_chat_path (append chat_path to base_url)",
            p.get("append_chat_path", True),
            choices=["true", "false"],
        )
        p["verify_tls"]  = _prompt("provider.verify_tls  (verify TLS certificates)", p.get("verify_tls", True), choices=["true", "false"])
        p["stream"]      = _prompt("provider.stream",                       p.get("stream", True),   choices=["true", "false"])
        p["temperature"] = _prompt("provider.temperature",                  p.get("temperature", 0.0))

        print("\n\033[36m\u2500\u2500 Runtime \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\033[0m")
        r = raw.setdefault("runtime", {})
        r["permission_mode"]       = _prompt("runtime.permission_mode", r.get("permission_mode", "workspace-write"),
                                             choices=["read-only", "workspace-write", "danger-full-access", "prompt", "allow"])
        r["max_iterations"]        = _prompt("runtime.max_iterations",        r.get("max_iterations", 12))
        r["shell_timeout_seconds"] = _prompt("runtime.shell_timeout_seconds", r.get("shell_timeout_seconds", 30))
        r["include_git_context"]   = _prompt("runtime.include_git_context",   r.get("include_git_context", True),  choices=["true", "false"])
        r["config_dump_in_prompt"] = _prompt("runtime.config_dump_in_prompt", r.get("config_dump_in_prompt", True), choices=["true", "false"])

        print("\n\033[36m\u2500\u2500 VS Code bridge \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\033[0m")
        v = raw.setdefault("vscode", {})
        v["auto_start_backend"]       = _prompt("vscode.auto_start_backend",       v.get("auto_start_backend", True),  choices=["true", "false"])
        v["python_command"]           = _prompt("vscode.python_command",           v.get("python_command", ""))
        v["startup_timeout_seconds"]  = _prompt("vscode.startup_timeout_seconds",  v.get("startup_timeout_seconds", 15))

        print("\n\033[36m\u2500\u2500 Sandbox \u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\u2500\033[0m")
        s = raw.setdefault("sandbox", {})
        s["enabled"]          = _prompt("sandbox.enabled          (null=auto)", s.get("enabled", None),
                                        choices=["null", "true", "false"])
        if s["enabled"] == "null":
            s["enabled"] = None
        s["filesystem_mode"]  = _prompt("sandbox.filesystem_mode",              s.get("filesystem_mode", "workspace-only"),
                                        choices=["workspace-only", "read-only", "unrestricted"])
        s["network_isolation"] = _prompt("sandbox.network_isolation (null=auto)", s.get("network_isolation", None),
                                         choices=["null", "true", "false"])
        if s["network_isolation"] == "null":
            s["network_isolation"] = None

    except KeyboardInterrupt:
        print("\n\nCancelled \u2014 no changes saved.")
        return 1

    path.write_text(dump_yaml(raw), encoding="utf-8")
    print(f"\n\033[32m\u2713 Config saved to {path}\033[0m\n")
    ok, message = _test_api_connection(args.config_path, workspace=Path.cwd().resolve())
    if ok:
        print(render_success(f"API check passed. {message}"))
    else:
        print(render_warning(f"API check failed: {message}"))
        print(render_info("You can keep secrets in YUCODE_API_KEY or .yucode/settings.local.yml instead of tracked files."))
        print()
    return 0


def handle_show_config(args: argparse.Namespace) -> int:
    path = ensure_default_config(resolve_config_path(args.config_path))
    print(path.read_text(encoding="utf-8"), end="")
    return 0


def handle_config_path(args: argparse.Namespace) -> int:
    print(resolve_config_path(args.config_path))
    return 0


def handle_bridge(args: argparse.Namespace) -> int:
    return BridgeServer().serve_forever()


_DEFAULT_YUCODE_MD = """\
# Project Instructions

<!-- YuCode reads this file to understand project-specific conventions. -->

## Overview

Describe your project here.

## Conventions

- Preferred language / framework:
- Code style:
- Testing approach:
"""

_DEFAULT_INIT_CONFIG = """\
provider:
  name: ""
  type: openai_compatible
  base_url: ""
  api_key: ""
  model: ""
  chat_path: /chat/completions
  append_chat_path: true
  verify_tls: true
  stream: true
  temperature: 0.0
  extra_headers: {}
  extra_body: {}
runtime:
  permission_mode: workspace-write
  max_iterations: 32
  max_worker_steps: 20
  orchestration_mode: auto
  parallel_workers: false
  shell_timeout_seconds: 30
  include_git_context: true
  config_dump_in_prompt: true
tools:
  allowed: []
  disabled: []
mcp:
  servers: []
hooks:
  pre_tool_use: []
  post_tool_use: []
plugins:
  enabled_plugins: []
  extra_dirs: []
sandbox:
  enabled: null
  namespace_restrictions: null
  network_isolation: null
  filesystem_mode: workspace-only
  allowed_mounts: []
instruction_files: []
"""


def handle_init(args: argparse.Namespace) -> int:
    from ..config.settings import state_dir
    target = Path(args.target).resolve()
    target.mkdir(parents=True, exist_ok=True)

    home_state = state_dir(target)
    home_state.mkdir(parents=True, exist_ok=True)
    print(f"  State dir {home_state}")

    config_path = home_state / "settings.yml"
    if not config_path.exists():
        config_path.write_text(_DEFAULT_INIT_CONFIG, encoding="utf-8")
        print(f"  Created {config_path}")
    else:
        print(f"  Exists  {config_path}")

    instruction_path = target / "YUCODE.md"
    if not instruction_path.exists():
        instruction_path.write_text(_DEFAULT_YUCODE_MD, encoding="utf-8")
        print(f"  Created {instruction_path}")
    else:
        print(f"  Exists  {instruction_path}")

    mcp_path = home_state / "mcp.yml"
    if not mcp_path.exists():
        mcp_path.write_text("servers: []\n", encoding="utf-8")
        print(f"  Created {mcp_path}")
    else:
        print(f"  Exists  {mcp_path}")

    skills_dir = home_state / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)

    for created in _ensure_project_support_files(target):
        if created not in {config_path, mcp_path, instruction_path}:
            print(f"  Created {created}")

    print(f"\n  Project initialized at {target}")
    print("\n  Getting started:")
    print("    1. Set YUCODE_API_KEY in .env or edit ~/.yucode/settings.yml")
    print("       (or run: yucode init-config --defaults  to write a template config)")
    print("    2. Edit YUCODE.md to describe your project conventions")
    print(f"    3. Run: yucode chat --workspace {target}")
    print()
    print("  Note: do NOT run `pip install .` from this directory.")
    print("        To install/update yucode, run: pip install yucode-agent")
    print()
    return 0


def _ensure_api_key(config_path: str | None) -> bool:
    if _has_configured_api_key(config_path, workspace=Path.cwd().resolve()):
        return True
    print(render_warning("No API key configured."))
    print(render_info("Run  yucode init-config  to set your provider, or export YUCODE_API_KEY.\n"))
    try:
        answer = input(f"  Run the config wizard now? {DIM}[Y/n]:{RESET} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print(f"\n{render_info('Cancelled.')}")
        return False
    if answer in ("", "y", "yes"):
        import argparse as _ap
        fake_args = _ap.Namespace(config_path=config_path)
        handle_init_config(fake_args)
        return _has_configured_api_key(config_path, workspace=Path.cwd().resolve())
    return False


# ---------------------------------------------------------------------------
# Chat handler
# ---------------------------------------------------------------------------

class _InteractiveEventHandler:
    """Stateful event handler with a two-line rolling progress window.

    At most two lines are visible during tool execution:
      Line 1 (done):  ✔ web_search  3 results
      Line 2 (doing): ⠹ web_fetch: https://example.com…

    When a tool finishes and a new one starts, line 2 slides up to
    line 1 and the new tool takes line 2.  The display is erased before
    assistant text begins streaming.
    """

    def __init__(self, *, streaming: bool = True) -> None:
        self.streaming = streaming
        self._progress = ProgressDisplay()
        self._turn_start: float = 0
        self._current_iteration = 0
        self._text_started = False
        self._in_coordinator: bool = False

    def __call__(self, event: dict[str, Any]) -> None:
        etype = event.get("type", "")
        if etype == "provider_info":
            pass
        elif etype == "phase_started":
            self._in_coordinator = True
            phase = event.get("phase", "")
            attempt = event.get("attempt")
            label = phase.replace("_", " ").title()
            if attempt:
                label += f" (attempt {attempt})"
            self._progress.set_thinking(f"Phase: {label}")
        elif etype == "worker_spawned":
            role = event.get("role", "?")
            task = event.get("task", "")[:60]
            idx = event.get("task_index", 0) + 1
            total = event.get("total_tasks", 1)
            counter = f" {idx}/{total}" if total > 1 else ""
            self._progress.set_thinking(f"Worker [{role}{counter}]: {task}")
        elif etype == "validation_result":
            self._progress.stop()
            passed = event.get("passed", False)
            attempt = event.get("attempt", "?")
            if passed:
                print(render_success(f"Validation passed (attempt {attempt})"))
            else:
                feedback = event.get("feedback", "")[:120]
                print(render_warning(f"Validation failed (attempt {attempt}): {feedback}"))
        elif etype == "retry_started":
            attempt = event.get("attempt", "?")
            self._progress.set_thinking(f"Retrying (attempt {attempt})")
        elif etype == "iteration_started":
            self._current_iteration = event.get("iteration", 1)
            if self._current_iteration == 1 and not self._in_coordinator:
                # Only reset turn state at the top level; inside a coordinator
                # worker this would wrongly clear _text_started and cause the
                # agent label to be printed mid-worker output.
                self._turn_start = time.monotonic()
                self._text_started = False
            if not self._in_coordinator and self._current_iteration == 1:
                # Only set the spinner on the first iteration.  On later
                # iterations the spinner already shows the last tool result
                # (e.g. "✔ web_search: 5 results") which is far more useful
                # than a generic "Thinking (iter N)" label.  The tool_call
                # event will update it again as soon as the next tool fires.
                self._progress.set_thinking("Thinking")
        elif etype == "tool_call":
            name = event.get("name", "?")
            arguments = event.get("arguments", "{}")
            label = compact_tool_start_label(name, arguments)
            self._progress.start_tool(label)
        elif etype == "tool_result":
            name = event.get("name", "?")
            content = event.get("content", "")
            _s = content.lstrip()
            is_err = _s.startswith('{\n  "error"') or _s.startswith('{"error"')
            result_line = compact_tool_result_line(name, content, is_error=is_err)
            self._progress.finish_tool(result_line)
        elif etype == "assistant_delta":
            if self._in_coordinator:
                # Worker sub-runtimes stream via this event too.  Suppress their
                # intermediate output — the coordinator's final_text is what the
                # user sees, delivered after `completed`.
                pass
            else:
                self._progress.stop()
                if not self._text_started:
                    print(agent_label(), end="")
                    self._text_started = True
                if self.streaming:
                    delta = event.get("delta", "")
                    sys.stdout.write(delta)
                    sys.stdout.flush()
        elif etype == "dedup_limit":
            self._progress.stop()
            tool = event.get("tool", "?")
            blocks = event.get("blocks_this_turn", 1)
            msg = f"Repeated call blocked: {tool}"
            if blocks > 1:
                msg += f" ({blocks}× this turn — agent may be stuck)"
            print(render_warning(msg), file=sys.stderr)
        elif etype == "stuck_exit":
            self._progress.stop()
            tool = event.get("tool", "?")
            blocks = event.get("blocks", 0)
            print(render_warning(
                f"Agent stuck on `{tool}` ({blocks}× blocked) — force-stopped.\n"
                "  Tip: rephrase your request, use /clear to reset, or break the task into smaller steps."
            ), file=sys.stderr)
            print()
        elif etype == "compaction":
            self._progress.stop()
            removed = event.get("removed", 0)
            print(render_info(f"Compacted {removed} messages to free context"))
        elif etype == "auto_compaction":
            self._progress.stop()
            removed = event.get("removed", 0)
            tokens = event.get("cumulative_input_tokens", 0)
            print(render_info(f"Auto-compacted {removed} messages ({tokens:,} cumulative input tokens)"))
        elif etype == "usage":
            pass
        elif etype == "completed":
            self._progress.stop()
            was_coordinator = self._in_coordinator
            self._in_coordinator = False
            if not self._text_started and not was_coordinator:
                # Non-coordinator, non-streaming: put the label here so the
                # chat loop's format_assistant_text() lands right after it.
                print(agent_label(), end="")
            print()


def _cli_event_callback(event: dict[str, Any]) -> None:
    """Single-turn event callback — same two-line rolling display."""
    progress = _cli_event_callback._progress  # type: ignore[attr-defined]
    etype = event.get("type", "")
    if etype == "phase_started":
        phase = event.get("phase", "")
        attempt = event.get("attempt")
        label = phase.replace("_", " ").title()
        if attempt:
            label += f" (attempt {attempt})"
        progress.set_thinking(f"Phase: {label}")
    elif etype == "worker_spawned":
        role = event.get("role", "?")
        task = event.get("task", "")[:60]
        idx = event.get("task_index", 0) + 1
        total = event.get("total_tasks", 1)
        counter = f" {idx}/{total}" if total > 1 else ""
        progress.set_thinking(f"Worker [{role}{counter}]: {task}")
    elif etype == "validation_result":
        progress.stop()
        passed = event.get("passed", False)
        attempt = event.get("attempt", "?")
        if passed:
            print(render_success(f"Validation passed (attempt {attempt})"))
        else:
            feedback = event.get("feedback", "")[:120]
            print(render_warning(f"Validation failed (attempt {attempt}): {feedback}"))
    elif etype == "retry_started":
        attempt = event.get("attempt", "?")
        progress.set_thinking(f"Retrying (attempt {attempt})")
    elif etype == "tool_call":
        name = event.get("name", "?")
        arguments = event.get("arguments", "{}")
        label = compact_tool_start_label(name, arguments)
        progress.start_tool(label)
    elif etype == "dedup_limit":
        progress.stop()
        tool = event.get("tool", "?")
        blocks = event.get("blocks_this_turn", 1)
        msg = f"Repeated call blocked: {tool}"
        if blocks > 1:
            msg += f" ({blocks}× this turn)"
        print(render_warning(msg), file=sys.stderr)
    elif etype == "stuck_exit":
        progress.stop()
        tool = event.get("tool", "?")
        blocks = event.get("blocks", 0)
        print(render_warning(
            f"Agent stuck on `{tool}` ({blocks}× blocked) — force-stopped.\n"
            "  Tip: rephrase your request or break the task into smaller steps."
        ), file=sys.stderr)
        print()
    elif etype == "tool_result":
        name = event.get("name", "?")
        content = event.get("content", "")
        _s = content.lstrip()
        is_err = _s.startswith('{\n  "error"') or _s.startswith('{"error"')
        result_line = compact_tool_result_line(name, content, is_error=is_err)
        progress.finish_tool(result_line)
    elif etype == "auto_compaction":
        progress.stop()
        removed = event.get("removed", 0)
        tokens = event.get("cumulative_input_tokens", 0)
        print(render_info(f"Auto-compacted {removed} messages ({tokens:,} cumulative input tokens)"))
    elif etype == "assistant_delta":
        pass
    elif etype == "completed":
        progress.stop()
        print()

_cli_event_callback._progress = ProgressDisplay()  # type: ignore[attr-defined]


def handle_default(args: argparse.Namespace) -> int:
    """Entry point for bare `yucode` with no subcommand — start interactive mode."""
    if not _ensure_api_key(args.config_path):
        return 1
    import argparse as _ap
    interactive_args = _ap.Namespace(
        workspace=getattr(args, "workspace", "."),
        config_path=args.config_path,
        resume=None,
        model=getattr(args, "model", None),
        allowed_tools=None,
        permission_mode=getattr(args, "permission_mode", None),
    )
    return _run_interactive(interactive_args)


def handle_chat(args: argparse.Namespace) -> int:
    if not _ensure_api_key(args.config_path):
        return 1
    if args.prompt is None:
        print(render_error(
            "`yucode chat` requires a prompt.\n"
            "  Usage:  yucode chat \"<your task>\"\n"
            "  For interactive mode run:  yucode"
        ), file=sys.stderr)
        return 1
    return _run_single_turn(args)


def _apply_cli_overrides(config: Any, args: argparse.Namespace) -> Any:
    from ..config import ProviderConfig, RuntimeOptions, ToolOptions
    changes: dict[str, Any] = {}
    if getattr(args, "model", None):
        old_provider = config.provider
        changes["provider"] = ProviderConfig(
            name=old_provider.name, type=old_provider.type,
            base_url=old_provider.base_url, api_key=old_provider.api_key,
            model=args.model, chat_path=old_provider.chat_path,
            append_chat_path=old_provider.append_chat_path,
            verify_tls=old_provider.verify_tls,
            stream=old_provider.stream, streaming_mode=old_provider.streaming_mode,
            temperature=old_provider.temperature,
            extra_headers=dict(old_provider.extra_headers),
            extra_body=dict(old_provider.extra_body),
        )
    if getattr(args, "permission_mode", None):
        old_rt = config.runtime
        changes["runtime"] = RuntimeOptions(
            permission_mode=args.permission_mode,
            max_iterations=old_rt.max_iterations,
            max_worker_steps=old_rt.max_worker_steps,
            orchestration_mode=old_rt.orchestration_mode,
            parallel_workers=old_rt.parallel_workers,
            max_tool_calls=old_rt.max_tool_calls,
            dedup_tool_threshold=old_rt.dedup_tool_threshold,
            auto_save_session=old_rt.auto_save_session,
            auto_resume_latest=old_rt.auto_resume_latest,
            compact_preserve_recent=old_rt.compact_preserve_recent,
            compact_token_threshold=old_rt.compact_token_threshold,
            shell_timeout_seconds=old_rt.shell_timeout_seconds,
            include_git_context=old_rt.include_git_context,
            config_dump_in_prompt=old_rt.config_dump_in_prompt,
        )
    if getattr(args, "allowed_tools", None) is not None:
        changes["tools"] = ToolOptions(
            allowed=list(args.allowed_tools),
            disabled=list(config.tools.disabled),
        )
    if not changes:
        return config
    return AppConfig(
        provider=changes.get("provider", config.provider),
        runtime=changes.get("runtime", config.runtime),
        tools=changes.get("tools", config.tools),
        mcp=config.mcp, vscode=config.vscode,
        instruction_files=config.instruction_files,
        hooks=config.hooks, plugins=config.plugins, sandbox=config.sandbox,
    )


def _try_auto_resume(workspace: Path, config: Any) -> Any:
    """Load the latest session if auto_resume_latest is enabled."""
    if not config.runtime.auto_resume_latest:
        return None
    from ..core.session import Session
    latest_path = Session.sessions_dir(workspace) / "latest.json"
    if not latest_path.is_file():
        return None
    try:
        return Session.load(latest_path)
    except Exception:  # noqa: BLE001
        return None


def _auto_save(runtime: AgentRuntime) -> None:
    """Save the current session as 'latest' for cross-chat continuity."""
    if not runtime.config.runtime.auto_save_session:
        return
    with suppress(Exception):
        runtime.save_session("latest")


def _run_single_turn(args: argparse.Namespace) -> int:
    from .commands import InputKind, parse_input

    parsed = parse_input(args.prompt, Path(args.workspace).resolve())
    if parsed.kind != InputKind.CHAT:
        return _dispatch_slash(parsed, args)

    workspace = Path(args.workspace).resolve()
    config = load_app_config(args.config_path, workspace=workspace)
    config = _apply_cli_overrides(config, args)

    session = None
    if getattr(args, "resume", None):
        from ..core.session import Session
        try:
            session = Session.load_from_workspace(workspace, args.resume)
        except FileNotFoundError:
            print(render_error(f"Session `{args.resume}` not found."), file=sys.stderr)
            return 1
    elif config.runtime.auto_resume_latest:
        session = _try_auto_resume(workspace, config)

    prompter = InteractivePermissionPrompter() if config.runtime.permission_mode == "prompt" else None
    runtime = AgentRuntime(workspace, config, session=session, permission_prompter=prompter)

    use_json = getattr(args, "json_output", False) or getattr(args, "output_format", "text") == "json"
    if use_json:
        events: list[dict[str, object]] = []
        summary = runtime.orchestrate(parsed.effective_prompt, event_callback=events.append)
        _auto_save(runtime)
        print(json.dumps({"final_text": summary.final_text, "iterations": summary.iterations, "events": events}, indent=2))
    else:
        _cli_event_callback._progress = ProgressDisplay()  # type: ignore[attr-defined]
        summary = runtime.orchestrate(parsed.effective_prompt, event_callback=_cli_event_callback)
        _cli_event_callback._progress.stop()  # type: ignore[attr-defined]
        _auto_save(runtime)
        if not summary.final_text.strip() and summary.usage.total_tokens() == 0:
            print(render_warning(
                "The provider returned an empty response with 0 tokens.\n"
                "  This usually means your provider configuration is wrong.\n"
                "  Checklist:\n"
                "    - Is YUCODE_API_KEY set (or api_key in settings.yml)?\n"
                "    - Is provider.base_url correct?\n"
                "    - Is provider.append_chat_path correct for your endpoint?\n"
                "    - Does this environment need provider.verify_tls: false?\n"
                "    - Is provider.model a valid model name for your provider?\n"
                "    - Does your provider support streaming? (try setting provider.stream: false)\n"
                "  Run `yucode doctor --workspace .` for diagnostics."
            ), file=sys.stderr)
        print(format_assistant_text(summary.final_text))
        print()
        print(format_cost_summary(summary.usage.to_dict()))
    return 0


_SLASH_COMMANDS = [
    "/help", "/status", "/config", "/tools", "/mcp", "/skills",
    "/clear", "/exit", "/quit", "/compact", "/diff", "/memory",
    "/resume", "/permissions", "/branch", "/commit", "/plugins",
    "/agents", "/save", "/metrics", "/cost", "/pr", "/log",
    "/worktree", "/stash", "/export", "/model", "/version",
]


def _make_completer(workspace: Path):
    def completer(text: str, state: int) -> str | None:
        if text.startswith("/"):
            options = [c + " " for c in _SLASH_COMMANDS if c.startswith(text)]
        elif text.startswith("@"):
            partial = text[1:]
            base = workspace / partial if partial else workspace
            parent = base.parent if partial and not base.is_dir() else base
            try:
                entries = sorted(parent.iterdir()) if parent.is_dir() else []
            except OSError:
                entries = []
            options = []
            for entry in entries:
                rel = str(entry.relative_to(workspace))
                candidate = "@" + rel + ("/" if entry.is_dir() else " ")
                if candidate.startswith(text):
                    options.append(candidate)
        else:
            options = []
        return options[state] if state < len(options) else None
    return completer


def _format_session_resume_info(session: Any) -> str:
    """Build a human-readable resume label from a session object."""
    msg_count = len(session.messages)
    # Find first user message for context
    first_user = ""
    for m in session.messages:
        if m.role == "user":
            raw = (m.content or "").strip()
            first_user = raw[:50] + ("…" if len(raw) > 50 else "")
            break
    age = ""
    if hasattr(session, "created_at") and session.created_at:
        import time as _time
        secs = _time.time() - session.created_at
        if secs < 3600:
            age = f"{int(secs // 60)}m ago"
        elif secs < 86400:
            age = f"{int(secs // 3600)}h ago"
        else:
            age = f"{int(secs // 86400)}d ago"
    parts = [f"{msg_count} msgs"]
    if age:
        parts.append(age)
    if first_user:
        parts.append(f'"{first_user}"')
    return "resumed: " + " · ".join(parts)


def _run_interactive(args: argparse.Namespace) -> int:
    from .commands import InputKind, parse_input

    workspace = Path(args.workspace).resolve()
    config = load_app_config(args.config_path, workspace=workspace)
    config = _apply_cli_overrides(config, args)

    session = None
    session_info = ""
    if getattr(args, "resume", None):
        from ..core.session import Session
        try:
            session = Session.load_from_workspace(workspace, args.resume)
            session_info = f"resumed `{args.resume}` ({len(session.messages)} msgs)"
        except FileNotFoundError:
            print(render_error(f"Session `{args.resume}` not found."), file=sys.stderr)
            return 1
    elif config.runtime.auto_resume_latest:
        session = _try_auto_resume(workspace, config)
        if session:
            session_info = _format_session_resume_info(session)

    prompter = InteractivePermissionPrompter() if config.runtime.permission_mode == "prompt" else None
    runtime = AgentRuntime(workspace, config, session=session, permission_prompter=prompter)

    try:
        import readline
        readline.set_completer(_make_completer(workspace))
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("tab: complete")
    except ImportError:
        pass

    from ..memory.prompting import MAX_INSTRUCTION_FILE_CHARS, MAX_TOTAL_INSTRUCTION_CHARS, discover_instruction_files
    _ifiles = discover_instruction_files(workspace, config.instruction_files or [])
    _remaining = MAX_TOTAL_INSTRUCTION_CHARS
    _ifile_labels: list[str] = []
    for _f in _ifiles:
        _cap = min(MAX_INSTRUCTION_FILE_CHARS, _remaining)
        _name = _f.path.name
        if _cap <= 0:
            _ifile_labels.append(f"{_name}[dropped]")
        elif len(_f.content) > _cap:
            _ifile_labels.append(f"{_name}[truncated]")
        else:
            _ifile_labels.append(_name)
        _remaining -= min(_cap, len(_f.content))
    print(startup_banner(
        workspace,
        model=config.provider.model or "(not set)",
        provider=config.provider.name or config.provider.type,
        permission_mode=config.runtime.permission_mode,
        session_info=session_info,
        instruction_files=_ifile_labels or None,
    ))

    handler = _InteractiveEventHandler(streaming=config.provider.streaming_mode != "no_stream")

    while True:
        try:
            line = input(user_prompt()).strip()
        except (EOFError, KeyboardInterrupt):
            _auto_save(runtime)
            print(f"\n{render_info('Bye.')}")
            return 0
        if not line:
            continue
        if line.lower() in ("exit", "quit", "bye"):
            _auto_save(runtime)
            print(render_info("Bye."))
            return 0
        parsed = parse_input(line, workspace)
        if parsed.kind == InputKind.SLASH:
            _dispatch_slash_interactive(parsed, config, workspace, runtime)
            continue
        try:
            summary = runtime.orchestrate(parsed.effective_prompt, event_callback=handler)
            if not summary.final_text.strip() and summary.usage.total_tokens() == 0:
                print(render_warning(
                    "Provider returned an empty response with 0 tokens. "
                    "Check your provider config or run `yucode doctor`."
                ), file=sys.stderr)
            if not handler._text_started:
                print(agent_label(), end="")
                print(format_assistant_text(summary.final_text))
            elif not config.provider.stream:
                print(format_assistant_text(summary.final_text))
            print()
            _auto_save(runtime)
        except AgentError as exc:
            handler._progress.stop()
            print(render_error(str(exc)))
        except Exception as exc:
            handler._progress.stop()
            print(render_error(f"Unexpected error: {exc}"))
        except KeyboardInterrupt:
            handler._progress.stop()
            print(f"\n{render_warning('Interrupted.')}")


def _dispatch_slash(parsed: Any, args: argparse.Namespace) -> int:
    from .commands import InputKind
    if parsed.kind != InputKind.SLASH:
        return 1
    return _handle_slash_command(parsed.command, parsed.arguments, args)


def _dispatch_slash_interactive(parsed: Any, config: Any, workspace: Path, runtime: Any) -> None:
    from .commands import InputKind
    if parsed.kind != InputKind.SLASH:
        return
    _handle_slash_command_interactive(parsed.command, parsed.arguments, config, workspace, runtime)


def _handle_slash_command(command: str, arguments: str, args: argparse.Namespace) -> int:
    if command == "help":
        _print_slash_help()
    elif command == "status":
        _print_status(args.config_path)
    elif command == "config":
        path = resolve_config_path(args.config_path)
        print(path.read_text(encoding="utf-8"), end="")
    elif command == "tools":
        config = load_app_config(args.config_path)
        workspace = Path(args.workspace).resolve()
        rt = AgentRuntime(workspace, config)
        for name in rt.tools.list_names():
            print(f"  {name}")
    elif command == "mcp":
        _print_mcp_list(args.config_path)
    elif command == "skills":
        workspace = Path(args.workspace).resolve()
        for skill in list_skills(workspace):
            print(f"  {skill.name}: {skill.description}")
    else:
        print(f"Unknown command: /{command}")
        return 1
    return 0


def _handle_slash_command_interactive(command: str, arguments: str, config: Any, workspace: Path, runtime: Any) -> None:
    from .commands import (
        export_session,
        list_instruction_files,
        run_git_branch,
        run_git_commit,
        run_git_diff,
        run_git_log,
        run_git_pr,
        run_git_stash,
        run_git_worktree,
    )

    if command == "help":
        _print_slash_help()
    elif command == "status":
        print(section_header("Status"))
        usage = runtime.session.usage
        print(f"  {DIM}provider{RESET}     {config.provider.name}/{config.provider.model}")
        print(f"  {DIM}workspace{RESET}    {workspace}")
        print(f"  {DIM}permissions{RESET}  {config.runtime.permission_mode}")
        print(f"  {DIM}streaming{RESET}    {config.provider.streaming_mode}")
        print(f"  {DIM}tools{RESET}        {len(runtime.tools.list_names())}")
        print(f"  {DIM}mcp servers{RESET}  {len(config.mcp)}")
        print(f"  {DIM}messages{RESET}     {len(runtime.session.messages)}")
        print(f"  {DIM}est. tokens{RESET}  {runtime.estimated_tokens():,}")
        print()
        print(format_cost_summary(usage.to_dict()))
    elif command == "config":
        print(section_header("Config"))
        path = resolve_config_path()
        print(f"  {DIM}path: {path}{RESET}\n")
        print(path.read_text(encoding="utf-8"), end="")
    elif command == "tools":
        print(section_header("Tools"))
        tools = runtime.tools.list_names()
        for name in tools:
            risk = runtime.tools.risk_level_for(name).value
            risk_color = THEME.success if risk == "low" else (THEME.warning if risk == "medium" else THEME.error)
            print(f"  ⚡ {name} {risk_color}[{risk}]{RESET}")
        print(f"\n  {DIM}{len(tools)} tools registered{RESET}")
    elif command == "mcp":
        print(section_header("MCP Servers"))
        if config.mcp:
            for server in config.mcp:
                transport_note = f" {DIM}[{server.transport}]{RESET}" if server.transport != "stdio" else ""
                print(f"  🔌 {BOLD}{server.name}{RESET}{transport_note}: {server.command} {' '.join(server.args)}")
        else:
            print(f"  {DIM}No MCP servers configured.{RESET}")
            print(f"  {DIM}Run: yucode mcp-add <name> <command>{RESET}")
    elif command == "skills":
        print(section_header("Skills"))
        found = list_skills(workspace)
        if found:
            for skill in found:
                desc = f" — {skill.description}" if skill.description else ""
                print(f"  📚 {skill.name}{desc}")
        else:
            print(f"  {DIM}No skills found.{RESET}")
    elif command == "compact":
        result = runtime.compact()
        if result.removed_message_count > 0:
            print(render_success(f"Compacted {result.removed_message_count} messages"))
            print(render_info(f"Estimated tokens now: {runtime.estimated_tokens():,}"))
        else:
            print(render_info("Nothing to compact."))
    elif command == "diff":
        print(section_header("Git Diff"))
        print(run_git_diff(workspace))
    elif command == "memory":
        files = list_instruction_files(workspace)
        from ..core.session import Session
        sessions = Session.list_sessions(workspace)
        found_skills = list_skills(workspace)
        print(format_memory_display(
            workspace=workspace,
            instruction_files=files,
            sessions=sessions,
            skills=found_skills,
            estimated_tokens=runtime.estimated_tokens(),
            session_messages=len(runtime.session.messages),
            metrics=runtime.metrics.to_dict() if hasattr(runtime, "metrics") else None,
        ))
    elif command == "resume":
        from ..core.session import Session
        sessions = Session.list_sessions(workspace)
        if not sessions:
            print(render_info("No saved sessions found."))
        else:
            print(section_header("Saved Sessions"))
            for s in sessions[:10]:
                secs = time.time() - float(s.get("created_at") or 0)
                if secs < 3600:
                    age = f"{int(secs // 60)}m ago"
                elif secs < 86400:
                    age = f"{int(secs // 3600)}h ago"
                else:
                    age = f"{int(secs // 86400)}d ago"
                title = s.get("title", "")
                title_part = f'  "{title[:40]}"' if title else ""
                print(f"  💾 {s['id']:12s}  {DIM}{age:8s}  {s['message_count']} msgs{title_part}{RESET}")
            if arguments.strip():
                sid = arguments.strip()
                try:
                    loaded = Session.load_from_workspace(workspace, sid)
                    runtime.session = loaded
                    print(render_success(f"Resumed session `{sid}` ({len(loaded.messages)} messages)"))
                except FileNotFoundError:
                    print(render_error(f"Session `{sid}` not found."))
    elif command == "permissions":
        print(render_info(f"Current mode: {BOLD}{config.runtime.permission_mode}{RESET}"))
    elif command == "branch":
        print(section_header("Git Branches"))
        print(run_git_branch(workspace))
    elif command == "commit":
        result = run_git_commit(workspace, arguments)
        if "error" in result.lower() or "fatal" in result.lower():
            print(render_error(result))
        else:
            print(render_success(result))
    elif command == "plugins":
        from ..plugins import PluginManager
        print(section_header("Plugins"))
        pm = PluginManager(workspace)
        installed = pm.list_installed()
        if installed:
            for p in installed:
                status_icon = THEME.success + "●" if p.get("enabled") else THEME.muted + "○"
                desc = p.get("description", "")
                print(f"  {status_icon}{RESET} {p['name']} {DIM}{desc}{RESET}")
        else:
            print(f"  {DIM}No plugins installed.{RESET}")
    elif command == "agents":
        print(render_info("Sub-agent tool is available as `agent` in the tool list."))
        print(render_info("Use it in your prompt to delegate tasks."))
    elif command == "save":
        path = runtime.save_session(arguments.strip() or None)
        print(render_success(f"Session saved to {path}"))
    elif command == "clear":
        from ..core.session import Usage
        runtime.session.messages.clear()
        runtime.session.usage = Usage()
        print(render_success("Session cleared."))
    elif command in ("exit", "quit"):
        raise EOFError
    elif command == "metrics":
        _show_metrics(runtime)
    elif command == "cost":
        print(format_cost_summary(runtime.session.usage.to_dict()))
    elif command == "log":
        print(section_header("Git Log"))
        count = int(arguments.strip()) if arguments.strip().isdigit() else 10
        print(run_git_log(workspace, count))
    elif command == "stash":
        action = arguments.strip() or "list"
        result = run_git_stash(workspace, action)
        print(render_info(result) if "error" not in result.lower() else render_error(result))
    elif command == "pr":
        print(section_header("Pull Request"))
        print(run_git_pr(workspace))
    elif command == "worktree":
        print(section_header("Worktrees"))
        print(run_git_worktree(workspace, arguments.strip()))
    elif command == "export":
        fmt = arguments.strip() or "md"
        path = export_session(workspace, runtime.session.messages, fmt)
        print(render_success(f"Session exported to {path}"))
    elif command == "model":
        if arguments.strip():
            new_model = arguments.strip()
            old_model = config.provider.model
            from ..config import AppConfig, ProviderConfig
            new_provider = ProviderConfig(
                name=config.provider.name, type=config.provider.type,
                base_url=config.provider.base_url, api_key=config.provider.api_key,
                model=new_model, chat_path=config.provider.chat_path,
                append_chat_path=config.provider.append_chat_path,
                verify_tls=config.provider.verify_tls,
                stream=config.provider.stream, streaming_mode=config.provider.streaming_mode,
                temperature=config.provider.temperature,
                extra_headers=dict(config.provider.extra_headers),
                extra_body=dict(config.provider.extra_body),
            )
            new_config = AppConfig(
                provider=new_provider, runtime=config.runtime,
                tools=config.tools, mcp=config.mcp, vscode=config.vscode,
                instruction_files=config.instruction_files,
                hooks=config.hooks, plugins=config.plugins, sandbox=config.sandbox,
            )
            runtime.config = new_config
            from ..core.providers import OpenAICompatibleProvider
            runtime.provider = OpenAICompatibleProvider(new_provider)
            print(render_success(f"Model switched: {old_model} → {new_model}"))
        else:
            print(render_info(f"Current model: {BOLD}{config.provider.model}{RESET}"))
            print(f"  {DIM}provider{RESET}        {config.provider.name}")
            print(f"  {DIM}base_url{RESET}        {config.provider.base_url}")
            print(f"  {DIM}chat_path{RESET}       {config.provider.chat_path}")
            print(f"  {DIM}append_path{RESET}     {config.provider.append_chat_path}")
            print(f"  {DIM}verify_tls{RESET}      {config.provider.verify_tls}")
            print(f"  {DIM}stream{RESET}          {config.provider.stream}")
            print(f"  {DIM}streaming_mode{RESET}  {config.provider.streaming_mode}")
            print(f"\n  {DIM}Usage: /model <model-name> to switch{RESET}")
    elif command == "version":
        from .. import __version__
        print(render_info(f"YuCode v{__version__}"))
    else:
        print(render_error(f"Unknown command: /{command}"))


def _show_metrics(runtime: Any) -> None:
    """Display tool usage and session metrics."""
    print(section_header("Metrics"))
    metrics = runtime.metrics.to_dict()
    tools = metrics.get("tools", {})
    if tools:
        print(f"\n  {BOLD}Tool Usage{RESET}")
        for name, m in sorted(tools.items(), key=lambda x: x[1].get("call_count", 0), reverse=True):
            count = m.get("call_count", 0)
            avg = m.get("avg_duration_ms", 0)
            errs = m.get("error_count", 0)
            err_note = f" {THEME.error}({errs} errors){RESET}" if errs else ""
            print(f"    ⚡ {name}: {count}× avg {avg:.0f}ms{err_note}")
    session = metrics.get("session", {})
    print(f"\n  {BOLD}Session{RESET}")
    print(f"    iterations:    {session.get('iterations', 0)}")
    print(f"    input tokens:  {session.get('total_input_tokens', 0):,}")
    print(f"    output tokens: {session.get('total_output_tokens', 0):,}")
    security = metrics.get("security_events", [])
    if security:
        print(f"\n  {BOLD}Security Events{RESET}")
        for ev in security:
            print(f"    🛡️  {ev['event_type']} → {ev['tool_name']}: {ev.get('detail', '')[:60]}")


def _print_slash_help() -> None:
    print(section_header("Commands"))
    _cmds = [
        ("/help",        "Show this help message"),
        ("/status",      "Runtime status — model, tokens, messages"),
        ("/model [name]","Show or switch model in-session"),
        ("/memory",      "Show all memory & context info"),
        ("/tools",       "List available tools with risk levels"),
        ("/compact",     "Compact conversation to free context"),
        ("/metrics",     "Show tool usage and session metrics"),
        ("/cost",        "Show token usage breakdown"),
        ("/diff",        "Show git diff"),
        ("/log [N]",     "Show git log (last N commits)"),
        ("/branch",      "Show git branches"),
        ("/commit MSG",  "Git add -A and commit"),
        ("/pr",          "Show PR readiness (unpushed commits)"),
        ("/stash",       "Git stash list/push/pop"),
        ("/worktree",    "Git worktree list/add/remove"),
        ("/config",      "Show current configuration"),
        ("/mcp",         "List MCP servers"),
        ("/skills",      "List discovered skills"),
        ("/plugins",     "List installed plugins"),
        ("/permissions", "Show current permission mode"),
        ("/resume [ID]", "List or resume a saved session"),
        ("/save [ID]",   "Save current session"),
        ("/export [md]", "Export session to file (md or json)"),
        ("/doctor",      "Run preflight diagnostics"),
        ("/agents",      "Show sub-agent tool info"),
        ("/version",     "Show YuCode version"),
        ("/clear",       "Clear conversation history"),
        ("/exit",        "Exit interactive mode"),
    ]
    for cmd, desc in _cmds:
        print(f"  {THEME.prompt_you}{cmd:16s}{RESET} {DIM}{desc}{RESET}")
    print(f"\n  {DIM}Use @path to include workspace files as context.{RESET}")


def _print_status(config_path: str | None) -> None:
    config = load_app_config(config_path)
    print(section_header("Status"))
    print(f"  {DIM}provider{RESET}     {config.provider.name}/{config.provider.model}")
    print(f"  {DIM}permissions{RESET}  {config.runtime.permission_mode}")
    print(f"  {DIM}max_iters{RESET}    {config.runtime.max_iterations}")
    print(f"  {DIM}mcp servers{RESET}  {len(config.mcp)}")


# ---------------------------------------------------------------------------
# MCP CLI handlers
# ---------------------------------------------------------------------------

def handle_mcp_list(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    _print_mcp_list(args.config_path, workspace)
    return 0


def _print_mcp_list(config_path: str | None, workspace: Path | None = None) -> None:
    config = load_app_config(config_path, workspace=workspace)
    if not config.mcp:
        print("No MCP servers configured.")
        print("Add one with: yucode mcp-add <name> <command> [args...]")
        return
    print(f"MCP servers ({len(config.mcp)}):")
    for server in config.mcp:
        transport_note = f" [{server.transport}]" if server.transport != "stdio" else ""
        cmd = f"{server.command} {' '.join(server.args)}".strip()
        print(f"  {server.name}{transport_note}: {cmd}")


def handle_mcp_add(args: argparse.Namespace) -> int:
    server = McpServerConfig(
        name=args.name,
        transport=args.transport,
        command=args.mcp_command,
        args=args.args,
    )
    workspace = Path(args.workspace).resolve()
    try:
        path = add_mcp_server_to_config(server, args.config_path, workspace=workspace)
        print(f"Added MCP server `{args.name}` to {path}")
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def handle_mcp_remove(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    try:
        path = remove_mcp_server_from_config(args.name, args.config_path, workspace=workspace)
        print(f"Removed MCP server `{args.name}` from {path}")
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


def handle_mcp_validate(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    config = load_app_config(args.config_path, workspace=workspace)
    if not config.mcp:
        print("No MCP servers configured.")
        return 0
    from ..plugins.mcp import StdioMcpClient
    errors = 0
    for server_config in config.mcp:
        print(f"  {server_config.name}...", end="", flush=True)
        if server_config.transport != "stdio":
            print(f" SKIP (transport `{server_config.transport}` not implemented)")
            continue
        try:
            client = StdioMcpClient(server_config)
            tools = client.list_tools()
            print(f" OK ({len(tools)} tools)")
        except Exception as exc:  # noqa: BLE001
            print(f" FAIL: {exc}")
            errors += 1
    return 1 if errors else 0


# ---------------------------------------------------------------------------
# MCP preset catalog
# ---------------------------------------------------------------------------

_PYTHON_BIN = sys.executable or "python3"

MCP_PRESETS: dict[str, dict[str, Any]] = {
    "excel": {
        "description": "Read/write Excel files (openpyxl)",
        "command": _PYTHON_BIN,
        "args": ["-m", "coding_agent.plugins.mcp_servers.excel_mcp"],
        "deps": ["openpyxl"],
        "pip_extra": "excel",
    },
    "word": {
        "description": "Read/write Word documents (python-docx)",
        "command": _PYTHON_BIN,
        "args": ["-m", "coding_agent.plugins.mcp_servers.docx_mcp"],
        "deps": ["docx"],
        "pip_extra": "word",
    },
    "pdf": {
        "description": "Extract text and tables from PDFs (pdfplumber)",
        "command": _PYTHON_BIN,
        "args": ["-m", "coding_agent.plugins.mcp_servers.pdf_mcp"],
        "deps": ["pdfplumber"],
        "pip_extra": "pdf",
    },
    "finance": {
        "description": "Stock quotes, financials, history (yfinance + pandas)",
        "command": _PYTHON_BIN,
        "args": ["-m", "coding_agent.plugins.mcp_servers.finance_mcp"],
        "deps": ["yfinance", "pandas"],
        "pip_extra": "finance",
    },
}


def _check_python_dep(module_name: str) -> bool:
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def handle_mcp_preset(args: argparse.Namespace) -> int:
    if args.preset_name is None:
        print("Available MCP presets:\n")
        for name, preset in MCP_PRESETS.items():
            deps = preset.get("deps", [])
            if deps:
                status_parts = []
                for dep in deps:
                    ok = _check_python_dep(dep)
                    status_parts.append(f"{dep}:{'ok' if ok else 'missing'}")
                status = " | ".join(status_parts)
            else:
                status = "ready"
            extra = f"  (pip install yucode-agent[{preset['pip_extra']}])" if preset.get("pip_extra") else ""
            print(f"  {name:20s} {preset['description']:50s} [{status}]{extra}")
        print("\nUsage: yucode mcp-preset <name> [args...]")
        return 0

    name = args.preset_name
    if name not in MCP_PRESETS:
        print(f"Error: Unknown preset `{name}`. Run `yucode mcp-preset` to see all.", file=sys.stderr)
        return 1

    preset = MCP_PRESETS[name]
    missing = [d for d in preset.get("deps", []) if not _check_python_dep(d)]
    if missing:
        extra = preset.get("pip_extra", "")
        install_cmd = f"pip install yucode-agent[{extra}]" if extra else f"pip install {' '.join(missing)}"
        print(f"Missing dependencies: {', '.join(missing)}")
        print(f"Install with: {install_cmd}")
        return 1

    server_args = list(preset["args"])
    if preset.get("needs_path_arg"):
        if args.preset_args:
            server_args.extend(args.preset_args)
        else:
            workspace = Path(args.workspace).resolve()
            server_args.append(str(workspace))

    server = McpServerConfig(
        name=name,
        transport="stdio",
        command=preset["command"],
        args=server_args,
    )
    workspace = Path(args.workspace).resolve()
    try:
        path = add_mcp_server_to_config(server, args.config_path, workspace=workspace)
        print(f"Added MCP preset `{name}` to {path}")
    except ConfigError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


# ---------------------------------------------------------------------------
# Skills handler
# ---------------------------------------------------------------------------

def handle_skills(args: argparse.Namespace) -> int:
    workspace = Path(args.workspace).resolve()
    found = list_skills(workspace)
    output_json = getattr(args, "output_format", "text") == "json"
    if output_json:
        print(json.dumps({"skills": [{"name": s.name, "description": s.description, "path": str(s.path)} for s in found]}, indent=2))
        return 0
    if not found:
        print("No skills found.")
        print("Create skills in .yucode/skills/<name>/SKILL.md or .claw/skills/<name>/SKILL.md")
        return 0
    print(f"Skills ({len(found)}):")
    for skill in found:
        print(f"  {skill.name}: {skill.description}")
    return 0


# ---------------------------------------------------------------------------
# Status handler
# ---------------------------------------------------------------------------

def handle_status(args: argparse.Namespace) -> int:
    output_json = getattr(args, "output_format", "text") == "json"
    config = load_app_config(args.config_path)
    if output_json:
        data = {
            "provider": config.provider.name,
            "model": config.provider.model,
            "permission_mode": config.runtime.permission_mode,
            "max_iterations": config.runtime.max_iterations,
            "orchestration_mode": config.runtime.orchestration_mode,
            "mcp_server_count": len(config.mcp),
            "tools_allowed": list(config.tools.allowed) if config.tools.allowed else None,
            "tools_disabled": list(config.tools.disabled) if config.tools.disabled else None,
        }
        print(json.dumps(data, indent=2))
    else:
        _print_status(args.config_path)
    return 0


def handle_system_prompt(args: argparse.Namespace) -> int:
    from datetime import datetime

    from ..memory.prompting import PromptAssembler, discover_project_context

    workspace = Path(args.cwd).resolve()
    config = load_app_config(args.config_path, workspace=workspace)
    date_text = args.date or datetime.now().strftime("%Y-%m-%d")
    project_context = discover_project_context(
        workspace, current_date=date_text,
        include_git_context=config.runtime.include_git_context,
        explicit_instruction_files=config.instruction_files,
    )
    prompt = PromptAssembler(config, project_context).render()

    output_json = getattr(args, "output_format", "text") == "json"
    if output_json:
        print(json.dumps({"system_prompt": prompt, "workspace": str(workspace), "date": date_text}))
    else:
        print(prompt)
    return 0


def handle_version(args: argparse.Namespace) -> int:
    from .. import __version__
    output_json = getattr(args, "output_format", "text") == "json"
    if output_json:
        print(json.dumps({"version": __version__}))
    else:
        print(f"YuCode v{__version__}")
    return 0


def handle_doctor(args: argparse.Namespace) -> int:
    """Run preflight diagnostics -- ported from claw-code-main ``claw doctor``."""
    import shutil
    workspace = Path(args.workspace).resolve()
    output_json = getattr(args, "output_format", "text") == "json"

    checks: list[dict[str, Any]] = []

    rg_path = shutil.which("rg")
    checks.append({
        "name": "ripgrep",
        "status": "ok" if rg_path else "missing",
        "summary": f"Found at {rg_path}" if rg_path else "ripgrep (rg) not found on PATH; grep_search tool will not work",
    })

    config_path = resolve_config_path(getattr(args, "config_path", None))
    config_ok = config_path.is_file()
    checks.append({
        "name": "config_file",
        "status": "ok" if config_ok else "missing",
        "summary": f"Config at {config_path}" if config_ok else f"No config file at {config_path}",
    })

    try:
        config = load_app_config(getattr(args, "config_path", None), workspace=workspace)
        api_key_ok = bool(config.provider.api_key)
    except Exception as exc:
        api_key_ok = False
        checks.append({
            "name": "config_parse",
            "status": "error",
            "summary": f"Config parse error: {exc}",
        })
        config = None

    checks.append({
        "name": "api_key",
        "status": "ok" if api_key_ok else "missing",
        "summary": "API key configured" if api_key_ok else "No API key found (set YUCODE_API_KEY or configure in settings)",
    })

    from ..config.settings import state_dir
    yucode_state = state_dir(workspace)
    checks.append({
        "name": "state_dir",
        "status": "ok" if yucode_state.is_dir() else "info",
        "summary": f"State directory at {yucode_state}" if yucode_state.is_dir() else f"State directory will be created at {yucode_state}",
    })

    if config and config.mcp:
        mcp_names = [s.name for s in config.mcp]
        checks.append({
            "name": "mcp_servers",
            "status": "ok",
            "summary": f"MCP servers configured: {', '.join(mcp_names)}",
        })

    if config:
        probe_ok, probe_status, probe_message = _probe_provider_connection(
            getattr(args, "config_path", None),
            workspace=workspace,
            stream=False,
        )
        checks.append({
            "name": "provider_non_streaming",
            "status": "ok" if probe_ok and probe_status == "ok" else probe_status,
            "summary": probe_message,
        })
        if config.provider.stream:
            stream_ok, stream_status, stream_message = _probe_provider_connection(
                getattr(args, "config_path", None),
                workspace=workspace,
                stream=True,
            )
            checks.append({
                "name": "provider_streaming",
                "status": "ok" if stream_ok and stream_status == "ok" else stream_status,
                "summary": stream_message,
            })
        else:
            checks.append({
                "name": "provider_streaming",
                "status": "info",
                "summary": "Streaming is disabled in config (`provider.stream: false`).",
            })

    sandbox_info: dict[str, Any] = {"name": "sandbox", "status": "info"}
    try:
        from ..security.sandbox import SandboxStatus
        status = SandboxStatus.detect()
        sandbox_info["summary"] = f"In container: {status.in_container}"
        sandbox_info["details"] = {"in_container": status.in_container, "markers": status.markers}
    except Exception:
        sandbox_info["summary"] = "Sandbox detection unavailable"
    checks.append(sandbox_info)

    # Workspace Python-project check: yucode works in any directory, but running
    # `pip install .` here will fail if there is no pyproject.toml / setup.py.
    has_pyproject = (workspace / "pyproject.toml").is_file()
    has_setup = (workspace / "setup.py").is_file() or (workspace / "setup.cfg").is_file()
    if has_pyproject or has_setup:
        checks.append({
            "name": "workspace_python_project",
            "status": "ok",
            "summary": "Python project detected (pyproject.toml / setup.py present).",
        })
    else:
        checks.append({
            "name": "workspace_python_project",
            "status": "info",
            "summary": (
                "No pyproject.toml or setup.py in workspace — yucode works fine here. "
                "Do NOT run `pip install .` from this directory; "
                "install yucode itself with `pip install yucode-agent` from any other directory."
            ),
        })

    all_ok = all(c["status"] in ("ok", "info") for c in checks)

    if output_json:
        report = {
            "overall": "healthy" if all_ok else "degraded",
            "checks": checks,
            "workspace": str(workspace),
        }
        print(json.dumps(report, indent=2))
    else:
        print(f"\n  YuCode Doctor — {workspace}\n")
        for c in checks:
            icon = "✓" if c["status"] == "ok" else ("⚠" if c["status"] in ("warning", "missing") else "✗" if c["status"] == "error" else "·")
            print(f"  {icon}  {c['name']}: {c['summary']}")
        print()
        if all_ok:
            print("  All checks passed.\n")
        else:
            print("  Some checks need attention.\n")
    return 0 if all_ok else 1


def handle_serve(args: argparse.Namespace) -> int:
    from .server import run_server
    workspace = Path(args.workspace).resolve()
    print(f"Starting server on {args.host}:{args.port} (workspace: {workspace})")
    run_server(host=args.host, port=args.port, workspace_root=workspace)
    return 0


# ---------------------------------------------------------------------------
# Interactive permission prompter
# ---------------------------------------------------------------------------

class InteractivePermissionPrompter:
    """Terminal-based permission prompter for ``prompt`` mode."""

    def decide(self, request: Any) -> Any:
        from ..security.permissions import PermissionDecision
        print(f"\n  Permission required: {request.tool_name}")
        print(f"  Requires: {request.required_mode} (current: {request.current_mode})")
        if request.reason:
            print(f"  Reason: {request.reason}")
        try:
            answer = input("  Allow? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return PermissionDecision(False, "User cancelled")
        if answer in ("y", "yes"):
            return PermissionDecision(True)
        return PermissionDecision(False, "User denied")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
