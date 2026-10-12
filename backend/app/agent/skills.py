"""Agent Skills loader.

Skills follow the open format at https://agentskills.io/specification.
Discovery loads only name and description. The full SKILL.md is read when a
skill is activated.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)
_OVERRIDE: Path | None = None

QUALITY_SKILLS = ("dqx-define-checks", "dqx-apply-checks", "dataflow-quality")


def use_skills_path(path: str | Path | None) -> None:
    global _OVERRIDE
    _OVERRIDE = Path(path) if path else None


def skills_root() -> Path:
    if _OVERRIDE is not None:
        return _OVERRIDE
    configured = os.getenv("DATAFLOW_SKILLS_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "skills"


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    match = _FRONTMATTER.match(text)
    if match is None:
        raise ValueError("SKILL.md must start with YAML frontmatter.")
    fields: dict[str, str] = {}
    current: str | None = None
    chunks: list[str] = []
    for line in match.group(1).splitlines():
        if line.startswith(" ") and current:
            chunks.append(line.strip())
            continue
        if current:
            fields[current] = " ".join(chunks).strip()
            chunks = []
            current = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if value in {">", "|", ""}:
            current = key
            chunks = []
        else:
            fields[key] = value.strip("\"'")
    if current:
        fields[current] = " ".join(chunks).strip()
    body = text[match.end() :]
    return fields, body


def _validate_name(name: str, directory: str) -> None:
    if not _NAME.match(name) or len(name) > 64:
        raise ValueError(f"Skill name '{name}' does not match the Agent Skills format.")
    if name != directory:
        raise ValueError(f"Skill name '{name}' must match its directory '{directory}'.")


def list_skills() -> list[dict[str, str]]:
    """Stage 1: name and description only."""
    root = skills_root()
    found: list[dict[str, str]] = []
    if not root.is_dir():
        return found
    for path in sorted(root.iterdir()):
        skill_file = path / "SKILL.md"
        if not path.is_dir() or path.name.startswith(".") or not skill_file.is_file():
            continue
        fields, _body = _parse_frontmatter(skill_file.read_text())
        name = fields.get("name") or ""
        description = fields.get("description") or ""
        _validate_name(name, path.name)
        if not description or len(description) > 1024:
            raise ValueError(f"Skill '{name}' needs a description of 1 to 1024 characters.")
        found.append({"name": name, "description": description})
    return found


def load_skill(name: str) -> dict[str, Any]:
    """Stage 2: full instructions for one skill."""
    if not _NAME.match(name):
        raise ValueError(f"Unknown skill '{name}'.")
    path = skills_root() / name / "SKILL.md"
    if not path.is_file():
        known = ", ".join(item["name"] for item in list_skills()) or "none"
        raise ValueError(f"Unknown skill '{name}'. Available: {known}.")
    fields, body = _parse_frontmatter(path.read_text())
    _validate_name(fields.get("name") or "", name)
    return {
        "name": name,
        "description": fields.get("description") or "",
        "instructions": body.strip(),
    }


def discovery_text() -> str:
    skills = list_skills()
    if not skills:
        return ""
    lines = [
        "Agent skills (https://agentskills.io/). "
        "Call load_skill with a name when the task matches that description. "
        "Do not follow a skill from this list alone."
    ]
    for skill in skills:
        lines.append(f"- {skill['name']}: {skill['description']}")
    return "\n".join(lines) + "\n\n"


def quality_activation() -> dict[str, Any]:
    """Stage 2 for a copy: the product skill, plus the DQX skills it depends on."""
    loaded = [load_skill(name) for name in QUALITY_SKILLS]
    return {
        "names": [item["name"] for item in loaded],
        "instructions": loaded[-1]["instructions"],
    }


def validate_check_metadata(checks: list[dict[str, Any]]) -> None:
    """Fail fast, the same contract DQEngine.validate_checks enforces for metadata."""
    if not isinstance(checks, list):
        raise ValueError("DQX checks must be a list of metadata dicts.")
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("Each DQX check must be a dict.")
        name = check.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("Each DQX check needs a stable name.")
        if check.get("criticality") not in {"error", "warn"}:
            raise ValueError(f"Check {name} criticality must be error or warn.")
        body = check.get("check")
        if not isinstance(body, dict) or not str(body.get("function") or "").strip():
            raise ValueError(f"Check {name} needs check.function.")
        if not isinstance(body.get("arguments"), dict):
            raise ValueError(f"Check {name} needs check.arguments.")
