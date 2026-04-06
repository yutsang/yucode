"""Skill discovery and loading for YuCode.

Skills are directories containing a SKILL.md file with optional YAML
frontmatter (name, description).  They are discovered from well-known
roots in the workspace and user home, following Claw/Claude conventions
for compatibility.

Discovery roots (checked in order):
  <workspace>/.yucode/skills/
  <workspace>/.claw/skills/
  <workspace>/.codex/skills/
  ~/.yucode/skills/
  ~/.claw/skills/
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SkillInfo:
    name: str
    description: str
    path: Path

    def load_body(self) -> str:
        text = self.path.read_text(encoding="utf-8")
        return _strip_frontmatter(text)


def list_skills(workspace: Path, extra_roots: list[str] | None = None) -> list[SkillInfo]:
    roots = _discover_skill_roots(workspace)
    if extra_roots:
        for root in extra_roots:
            roots.append(Path(root).expanduser().resolve())
    seen: set[str] = set()
    skills: list[SkillInfo] = []
    for root in roots:
        if not root.is_dir():
            continue
        for child in sorted(root.iterdir()):
            skill_file = child / "SKILL.md"
            if not skill_file.is_file():
                continue
            name = child.name
            if name in seen:
                continue
            seen.add(name)
            meta = _parse_frontmatter(skill_file.read_text(encoding="utf-8"))
            skills.append(SkillInfo(
                name=meta.get("name", name),
                description=str(meta.get("description", "")),
                path=skill_file,
            ))
    return skills


def load_skill(workspace: Path, name: str) -> SkillInfo | None:
    for skill in list_skills(workspace):
        if skill.name == name:
            return skill
    return None


def skill_summaries_for_prompt(workspace: Path) -> str:
    skills = list_skills(workspace)
    if not skills:
        return ""
    lines = ["# Available skills"]
    for skill in skills:
        desc = f" -- {skill.description}" if skill.description else ""
        lines.append(f"- {skill.name}{desc}")
    lines.append("")
    lines.append("Use the `load_skill` tool with a skill name to read its full instructions.")
    return "\n".join(lines)


def _discover_skill_roots(workspace: Path) -> list[Path]:
    resolved = workspace.resolve()
    home = Path.home()
    return [
        resolved / ".yucode" / "skills",
        resolved / ".claw" / "skills",
        resolved / ".codex" / "skills",
        home / ".yucode" / "skills",
        home / ".claw" / "skills",
    ]


_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, Any]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}
    result: dict[str, Any] = {}
    for line in match.group(1).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip().strip("'\"")
        result[key] = value
    return result


def _strip_frontmatter(text: str) -> str:
    match = _FRONTMATTER_RE.match(text)
    if match:
        return text[match.end():]
    return text
