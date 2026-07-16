"""`yucode init-memory` — pre-fill a user-scope memory with environment facts.

Scans `~/.gitconfig`, `$SHELL`, `platform`, and a few env vars to compose
an initial `user-profile` memory the model can rely on cross-session.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from ..memory.store import MemoryEntry, MemoryStore

USER_PROFILE_NAME = "user-profile"


def gather_facts() -> dict[str, str]:
    """Collect environment facts without making any network calls."""
    facts: dict[str, str] = {}
    if name := _git_config("user.name"):
        facts["git_name"] = name
    if email := _git_config("user.email"):
        facts["git_email"] = email
    facts["os"] = f"{platform.system()} {platform.release()}"
    facts["shell"] = os.environ.get("SHELL", "")
    facts["home"] = os.environ.get("HOME", "")
    facts["editor"] = os.environ.get("EDITOR", "")
    if path_tools := _detect_path_tools():
        facts["path_tools"] = ", ".join(path_tools)
    return {k: v for k, v in facts.items() if v}


def render_user_profile_body(facts: dict[str, str]) -> str:
    """Format gathered facts into a memory body."""
    lines = ["Auto-detected user environment (run `yucode init-memory --force` to refresh):", ""]
    label_map = [
        ("git_name", "Name"),
        ("git_email", "Email"),
        ("os", "OS"),
        ("shell", "Shell"),
        ("editor", "Editor"),
        ("home", "Home"),
        ("path_tools", "CLI tools on PATH"),
    ]
    for key, label in label_map:
        if key in facts:
            lines.append(f"- **{label}:** {facts[key]}")
    lines.append("")
    lines.append("**How to apply:** use this for default assumptions about the user's environment "
                 "(e.g. quoting in zsh vs bash, default editor for prompts, OS-specific paths). "
                 "Update or delete this memory if the user changes setup.")
    return "\n".join(lines)


def bootstrap_user_profile(workspace: Path, *, force: bool = False) -> tuple[MemoryEntry, bool]:
    """Write or refresh the user-profile memory. Returns (entry, was_new).

    Raises FileExistsError when not ``force`` and a `user-profile.md` already exists.
    """
    store = MemoryStore(workspace)
    existing = store.read(USER_PROFILE_NAME, "user")
    if existing and not force:
        raise FileExistsError(
            f"{existing.path} already exists. Re-run with `--force` to refresh from current environment."
        )
    facts = gather_facts()
    body = render_user_profile_body(facts)
    description = "Auto-detected user environment: name, email, OS, shell, editor, CLI tools"
    entry = store.save(USER_PROFILE_NAME, description, "user", body, scope="user")
    return entry, existing is None


# ---- helpers ---------------------------------------------------------------

def _git_config(key: str) -> str:
    try:
        result = subprocess.run(
            ["git", "config", "--get", key],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip()


_TOOLS_OF_INTEREST = (
    "rg", "fd", "fzf", "bat", "exa", "eza", "delta", "gh", "docker", "podman",
    "kubectl", "terraform", "tmux", "neovim", "nvim", "vim", "code", "cursor",
    "poetry", "uv", "rye", "pnpm", "yarn", "bun", "cargo", "rustc", "go",
    "node", "python", "python3", "pyenv", "pipx",
)


def _detect_path_tools() -> list[str]:
    return [t for t in _TOOLS_OF_INTEREST if shutil.which(t) is not None][:12]
