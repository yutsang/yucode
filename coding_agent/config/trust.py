"""Trust gate for repo-local config.

Repo-local config (``<workspace>/.yucode/settings*.yml``) can define hook
commands (arbitrary shell execution on every tool call) and MCP servers
(arbitrary subprocess commands) that would otherwise get merged into the
effective config and auto-execute just by running yucode inside a cloned or
opened repo -- no confirmation needed. ``sanitize_repo_local_overlay`` strips
those two categories from an untrusted repo-local overlay before it's merged;
``clamp_permission_overlay`` additionally makes a repo-local
sandbox/permission_mode overlay additive-only (it can only make the effective
policy more restrictive, never less), regardless of trust -- ported from
grok-build's folder-trust gate (hooks/MCP) and additive-only project sandbox
profile merge.

Scope is deliberately narrow: provider/runtime/tools/logging/etc. settings
from a repo-local overlay are NOT gated -- only the two concrete arbitrary-
code-execution vectors (hooks, mcp.servers) and the permission/sandbox
loosening vector.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..security.permissions import PERMISSION_ORDER, PermissionMode
from .simple_yaml import YamlError, load_yaml

_TRUST_STORE_PATH = Path.home() / ".yucode" / "trusted_dirs.json"

_GATED_KEYS = ("hooks", "mcp")


def _load_trusted(store_path: Path | None = None) -> set[str]:
    store = store_path or _TRUST_STORE_PATH
    if not store.is_file():
        return set()
    try:
        raw = json.loads(store.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    return {str(item) for item in raw} if isinstance(raw, list) else set()


def is_trusted(workspace: Path, *, store_path: Path | None = None) -> bool:
    return str(workspace.resolve()) in _load_trusted(store_path)


def _save_trusted(store: Path, trusted: set[str]) -> None:
    from ..core.atomic_io import atomic_write_text
    store.parent.mkdir(parents=True, exist_ok=True)
    # Atomic: a truncated trust store silently un-trusts everything.
    atomic_write_text(store, json.dumps(sorted(trusted), indent=2))


def trust_workspace(workspace: Path, *, store_path: Path | None = None) -> None:
    store = store_path or _TRUST_STORE_PATH
    trusted = _load_trusted(store)
    trusted.add(str(workspace.resolve()))
    _save_trusted(store, trusted)


def untrust_workspace(workspace: Path, *, store_path: Path | None = None) -> None:
    store = store_path or _TRUST_STORE_PATH
    trusted = _load_trusted(store)
    trusted.discard(str(workspace.resolve()))
    _save_trusted(store, trusted)


def list_trusted_workspaces(*, store_path: Path | None = None) -> list[str]:
    return sorted(_load_trusted(store_path))


def sanitize_repo_local_overlay(overlay: dict[str, Any], *, trusted: bool) -> dict[str, Any]:
    """Strip hook commands and MCP server definitions from a repo-local
    config overlay unless the workspace is trusted. Returns a new dict;
    does not mutate the input."""
    if trusted:
        return overlay
    stripped = dict(overlay)
    for key in _GATED_KEYS:
        stripped.pop(key, None)
    return stripped


def _mode_rank(mode: Any) -> int:
    return PERMISSION_ORDER.get(mode, PERMISSION_ORDER["danger-full-access"])


def _read_dict_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text) if path.suffix == ".json" else load_yaml(text)
    except (json.JSONDecodeError, YamlError):
        return {}
    return raw if isinstance(raw, dict) else {}


def describe_untrusted_repo_local_config(workspace: Path) -> str | None:
    """Human-readable one-line warning if *workspace* has repo-local hooks
    or MCP servers that are being withheld because the directory isn't
    trusted yet. None if there's nothing gated (already trusted, workspace
    IS the home directory, or there's simply no repo-local hooks/mcp)."""
    resolved = workspace.resolve()
    if is_trusted(resolved):
        return None
    yucode_dir = resolved / ".yucode"
    if yucode_dir == Path.home().resolve() / ".yucode":
        return None

    found_hooks = False
    found_mcp = False
    for name in ("settings.yml", "settings.json", "settings.local.yml", "settings.local.json"):
        raw = _read_dict_file(yucode_dir / name)
        if raw.get("hooks"):
            found_hooks = True
        mcp = raw.get("mcp")
        if isinstance(mcp, dict) and mcp.get("servers"):
            found_mcp = True
    if _read_dict_file(yucode_dir / "mcp.yml").get("servers"):
        found_mcp = True

    if not found_hooks and not found_mcp:
        return None
    kinds = " and ".join(k for k, present in (("hooks", found_hooks), ("MCP servers", found_mcp)) if present)
    return (
        f"This workspace has repo-local {kinds} in .yucode/ that were NOT applied because this "
        "directory isn't trusted yet. Run `yucode trust` to enable them if you trust this repo."
    )


def clamp_permission_overlay(overlay: dict[str, Any], base_mode: PermissionMode) -> dict[str, Any]:
    """A repo-local overlay's runtime.permission_mode can only move the
    effective mode to something with an equal-or-lower PERMISSION_ORDER rank
    than *base_mode* (the mode already configured at the user/home level) --
    it can request more restrictions, never fewer. Unconditional: applies
    even to a trusted repo, since trust only concerns whether hook/MCP
    commands are allowed to auto-execute, not whether a project can silently
    escalate its own permission ceiling."""
    runtime = overlay.get("runtime")
    if not isinstance(runtime, dict) or "permission_mode" not in runtime:
        return overlay
    requested = runtime["permission_mode"]
    if _mode_rank(requested) <= _mode_rank(base_mode):
        return overlay
    clamped = dict(overlay)
    clamped_runtime = dict(runtime)
    clamped_runtime["permission_mode"] = base_mode
    clamped["runtime"] = clamped_runtime
    return clamped
