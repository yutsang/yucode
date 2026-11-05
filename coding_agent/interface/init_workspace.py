"""`/init` and `yucode init` — bootstrap an AGENTS.md for a fresh workspace.

Scans the project to detect language, package manager, test command, build
command, and a few other signals, then writes a starter AGENTS.md the user
can edit. Idempotent: refuses to overwrite an existing file unless
``force=True``.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorkspaceProfile:
    """Detected facts about a workspace; used to render the AGENTS.md draft."""
    languages: list[str]
    package_manager: str | None
    test_command: str | None
    build_command: str | None
    run_command: str | None
    frameworks: list[str]
    has_tests_dir: bool
    notable_files: list[str]
    git_remote: str | None
    description: str | None


_PY_TEST_HINTS = ("pytest", "unittest", "trial")
_FRAMEWORK_HINTS = {
    "fastapi": "FastAPI",
    "flask": "Flask",
    "django": "Django",
    "react": "React",
    "vue": "Vue",
    "next": "Next.js",
    "express": "Express",
    "rails": "Rails",
    "spring": "Spring",
}


def detect_profile(workspace: Path) -> WorkspaceProfile:
    """Inspect *workspace* and return a structured profile."""
    workspace = workspace.resolve()
    languages: list[str] = []
    package_manager: str | None = None
    test_command: str | None = None
    build_command: str | None = None
    run_command: str | None = None
    frameworks: list[str] = []
    notable_files: list[str] = []

    # ---- Python ---------------------------------------------------------
    pyproj = workspace / "pyproject.toml"
    reqs = workspace / "requirements.txt"
    setup_py = workspace / "setup.py"
    if pyproj.exists() or reqs.exists() or setup_py.exists():
        languages.append("Python")
        for p in (pyproj, reqs, setup_py):
            if p.exists():
                notable_files.append(p.name)
        if pyproj.exists():
            text = pyproj.read_text(encoding="utf-8", errors="replace")
            if "poetry" in text and "[tool.poetry" in text:
                package_manager = "poetry"
            elif "[tool.hatch" in text:
                package_manager = "hatch"
            elif "[build-system]" in text and "setuptools" in text:
                package_manager = "pip / setuptools"
            for f, label in _FRAMEWORK_HINTS.items():
                if re.search(rf"\b{f}\b", text, re.IGNORECASE):
                    frameworks.append(label)
        if any((workspace / "tests").exists() and (workspace / "tests").is_dir() for _ in [0]):
            test_command = "python -m pytest tests/ -x -q"
        elif any((workspace / "test").exists() for _ in [0]):
            test_command = "python -m pytest test/ -x -q"

    # ---- Node / TypeScript ---------------------------------------------
    pkg_json = workspace / "package.json"
    if pkg_json.exists():
        notable_files.append("package.json")
        try:
            pkg = json.loads(pkg_json.read_text(encoding="utf-8"))
            if isinstance(pkg, dict):
                scripts = pkg.get("scripts", {}) or {}
                if "test" in scripts:
                    test_command = f"npm test  # {scripts['test'][:60]}"
                if "build" in scripts:
                    build_command = f"npm run build  # {scripts['build'][:60]}"
                if "dev" in scripts:
                    run_command = f"npm run dev  # {scripts['dev'][:60]}"
                elif "start" in scripts:
                    run_command = f"npm start  # {scripts['start'][:60]}"
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                for f, label in _FRAMEWORK_HINTS.items():
                    if any(f in d.lower() for d in deps):
                        frameworks.append(label)
        except json.JSONDecodeError:
            pass
        if (workspace / "pnpm-lock.yaml").exists():
            package_manager = "pnpm"
        elif (workspace / "yarn.lock").exists():
            package_manager = "yarn"
        elif (workspace / "bun.lockb").exists():
            package_manager = "bun"
        else:
            package_manager = package_manager or "npm"
        if (workspace / "tsconfig.json").exists():
            languages.append("TypeScript")
            notable_files.append("tsconfig.json")
        else:
            languages.append("JavaScript")

    # ---- Rust ----------------------------------------------------------
    if (workspace / "Cargo.toml").exists():
        languages.append("Rust")
        notable_files.append("Cargo.toml")
        package_manager = "cargo"
        test_command = "cargo test"
        build_command = "cargo build"

    # ---- Go ------------------------------------------------------------
    if (workspace / "go.mod").exists():
        languages.append("Go")
        notable_files.append("go.mod")
        package_manager = "go modules"
        test_command = "go test ./..."
        build_command = "go build ./..."

    # ---- Make ----------------------------------------------------------
    if (workspace / "Makefile").exists():
        notable_files.append("Makefile")
        if not test_command:
            test_command = "make test"
        if not build_command:
            build_command = "make build"

    has_tests_dir = (workspace / "tests").is_dir() or (workspace / "test").is_dir()

    # ---- Git remote ----------------------------------------------------
    git_remote = _git_origin(workspace)

    # ---- README description -------------------------------------------
    description = _readme_tagline(workspace)

    # de-dup while preserving order
    def _dedup(xs: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    return WorkspaceProfile(
        languages=_dedup(languages),
        package_manager=package_manager,
        test_command=test_command,
        build_command=build_command,
        run_command=run_command,
        frameworks=_dedup(frameworks),
        has_tests_dir=has_tests_dir,
        notable_files=_dedup(notable_files),
        git_remote=git_remote,
        description=description,
    )


def render_agents_md(profile: WorkspaceProfile, workspace: Path) -> str:
    """Render a starter AGENTS.md from a profile."""
    name = workspace.resolve().name
    lines: list[str] = [f"# {name}", ""]
    if profile.description:
        lines.append(profile.description)
        lines.append("")

    lines.append("## Stack")
    if profile.languages:
        lines.append(f"- Languages: {', '.join(profile.languages)}")
    if profile.frameworks:
        lines.append(f"- Frameworks: {', '.join(profile.frameworks)}")
    if profile.package_manager:
        lines.append(f"- Package manager: {profile.package_manager}")
    if profile.git_remote:
        lines.append(f"- Repo: {profile.git_remote}")
    lines.append("")

    if any([profile.test_command, profile.build_command, profile.run_command]):
        lines.append("## Common commands")
        if profile.run_command:
            lines.append(f"- Run dev:  `{profile.run_command}`")
        if profile.build_command:
            lines.append(f"- Build:    `{profile.build_command}`")
        if profile.test_command:
            lines.append(f"- Test:     `{profile.test_command}`")
        lines.append("")

    lines.append("## Notable files")
    if profile.notable_files:
        for f in profile.notable_files:
            lines.append(f"- `{f}`")
    else:
        lines.append("- _(scan found no manifest files — add notable paths here)_")
    lines.append("")

    lines.append("## Conventions")
    lines.append("- _(fill in: code style, branch policy, commit message format, review process)_")
    lines.append("")

    lines.append("## Notes for the agent")
    lines.append("- _(things you want the agent to always remember about this repo)_")
    lines.append("")
    return "\n".join(lines)


def write_agents_md(workspace: Path, *, force: bool = False, filename: str = "AGENTS.md") -> tuple[Path, bool]:
    """Generate and write AGENTS.md. Returns (path, was_overwritten).

    Raises FileExistsError when ``force`` is False and the file already exists.
    """
    target = workspace / filename
    if target.exists() and not force:
        raise FileExistsError(
            f"{target} already exists. Re-run with `--force` to overwrite, "
            "or rename the existing file first."
        )
    profile = detect_profile(workspace)
    content = render_agents_md(profile, workspace)
    target.write_text(content, encoding="utf-8")
    return target, target.exists() and force


# ---- helpers ---------------------------------------------------------------

def _git_origin(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(workspace), "config", "--get", "remote.origin.url"],
            capture_output=True, text=True, check=False, timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    out = result.stdout.strip()
    return out or None


def _readme_tagline(workspace: Path) -> str | None:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = workspace / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Skip past the first heading (assumed to be the project name) and
        # return the first non-empty content line that follows.
        lines = text.splitlines()
        past_heading = False
        for line in lines:
            stripped = line.strip()
            if not past_heading and stripped.startswith("#"):
                past_heading = True
                continue
            if past_heading and stripped and not stripped.startswith(("#", "[!", "<!")):
                return stripped[:200]
        # Fallback: first non-empty line at all
        for line in lines:
            stripped = line.strip()
            if stripped and not stripped.startswith(("#", "[!", "<!", "---")):
                return stripped[:200]
    return None
