"""Persistent lessons for the data agent.

A lesson starts weak. Repeating the same correction, or reusing the same keys
on the same column shape, raises its weight. User corrections apply immediately.
Patterns noticed from runs apply only after they have been seen twice.
Row values are never stored.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOCK = threading.Lock()
_OVERRIDE: Path | None = None
_MAX_WEIGHT = 8
_MAX_EPISODES = 80
_KINDS = {"skip_column", "not_null", "format", "json", "allowed_values", "primary_keys", "write_mode"}


def use_memory_path(path: str | Path | None) -> None:
    global _OVERRIDE
    _OVERRIDE = Path(path) if path else None


def memory_path() -> Path:
    if _OVERRIDE is not None:
        return _OVERRIDE
    configured = os.getenv("DATAFLOW_MEMORY_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "data" / "agent_memory.json"


def _empty() -> dict[str, Any]:
    return {"lessons": [], "episodes": []}


def _load() -> dict[str, Any]:
    path = memory_path()
    if not path.exists():
        return _empty()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("lessons", [])
    data.setdefault("episodes", [])
    return data


def _save(data: dict[str, Any]) -> None:
    path = memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _signature(columns: list[str] | None) -> list[str]:
    names = []
    for name in columns or []:
        text = str(name).strip().lower()
        if text and text not in names:
            names.append(text)
    return sorted(names)


def _lesson_key(kind: str, column: str, detail: str, signature: list[str]) -> str:
    return "|".join([kind, column.lower(), detail.strip().lower(), ",".join(signature)])


def remember(
    *,
    kind: str,
    column: str = "",
    detail: str = "",
    columns: list[str] | None = None,
    origin: str = "user",
    weight: int | None = None,
) -> dict[str, Any]:
    """Record a lesson. The same lesson again increases its weight by one."""
    if kind not in _KINDS:
        raise ValueError(f"Unknown lesson kind '{kind}'.")
    signature = _signature(columns)
    column_name = str(column or "").strip()
    detail_text = str(detail or "").strip()[:240]
    key = _lesson_key(kind, column_name, detail_text, signature)
    with _LOCK:
        data = _load()
        for lesson in data["lessons"]:
            if lesson.get("key") == key:
                current = int(lesson.get("weight") or 1)
                lesson["weight"] = min(_MAX_WEIGHT, current + 1 if weight is None else max(current, weight))
                lesson["updated_at"] = _now()
                _save(data)
                return dict(lesson)
        lesson = {
            "key": key,
            "kind": kind,
            "column": column_name,
            "detail": detail_text,
            "columns": signature,
            "origin": "user" if origin != "episode" else "episode",
            "weight": min(_MAX_WEIGHT, weight or 1),
            "updated_at": _now(),
        }
        data["lessons"].append(lesson)
        _save(data)
        return dict(lesson)


def _applies(lesson: dict[str, Any], columns: list[str]) -> bool:
    signature = set(lesson.get("columns") or [])
    current = set(_signature(columns))
    if signature and len(signature & current) / len(signature) < 0.6:
        return False
    column = str(lesson.get("column") or "").lower()
    if column and column not in current and lesson.get("kind") not in {"primary_keys", "write_mode"}:
        return False
    return True


def _ready(lesson: dict[str, Any]) -> bool:
    weight = int(lesson.get("weight") or 1)
    if lesson.get("origin") == "episode":
        return weight >= 2
    return weight >= 1


def recall(columns: list[str] | None = None) -> list[dict[str, Any]]:
    """Lessons that apply to these columns and are strong enough to use."""
    with _LOCK:
        data = _load()
    ready = [dict(lesson) for lesson in data["lessons"] if _ready(lesson)]
    if not columns:
        ready.sort(key=lambda item: int(item.get("weight") or 1), reverse=True)
        return ready[:12]
    matched = [lesson for lesson in ready if _applies(lesson, columns)]
    matched.sort(key=lambda item: int(item.get("weight") or 1), reverse=True)
    return matched[:12]


def prompt_text() -> str:
    lessons = recall()
    if not lessons:
        return ""
    lines = ["Lessons learned from earlier runs. Prefer these when the columns match. Episode lessons appear only after the same pattern was seen twice."]
    for lesson in lessons:
        column = lesson.get("column") or "the source"
        detail = lesson.get("detail") or ""
        weight = lesson.get("weight")
        lines.append(
            f"- {lesson.get('kind')} on {column} {detail} (weight {weight}, {lesson.get('origin')})".strip()
        )
    return "\n".join(lines) + "\n\n"


def observe_episode(
    *,
    source: str,
    destination: str,
    columns: list[str],
    primary_keys: list[str],
    write_mode: str,
    failing_checks: list[str] | None = None,
) -> None:
    """Remember a finished run. Keys and write mode become lessons on the second repeat."""
    signature = _signature(columns)
    episode = {
        "source": source[:200],
        "destination": destination[:240],
        "columns": signature,
        "primary_keys": [str(key) for key in primary_keys],
        "write_mode": write_mode,
        "failing_checks": [str(name)[:80] for name in (failing_checks or [])[:20]],
        "at": _now(),
    }
    with _LOCK:
        data = _load()
        data["episodes"].append(episode)
        data["episodes"] = data["episodes"][-_MAX_EPISODES:]
        _save(data)
    same_keys = [
        item
        for item in _load()["episodes"]
        if item.get("columns") == signature and item.get("primary_keys") == episode["primary_keys"]
    ]
    if len(same_keys) >= 2 and episode["primary_keys"]:
        remember(
            kind="primary_keys",
            detail=",".join(episode["primary_keys"]),
            columns=signature,
            origin="episode",
            weight=len(same_keys),
        )
    same_mode = [
        item
        for item in _load()["episodes"]
        if item.get("columns") == signature and item.get("write_mode") == write_mode and write_mode
    ]
    if len(same_mode) >= 2:
        remember(kind="write_mode", detail=write_mode, columns=signature, origin="episode", weight=len(same_mode))


def apply_lessons(checks: list[dict[str, Any]], lessons: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop checks a lesson skips, and add checks a lesson requires."""
    active = [lesson for lesson in lessons if _ready(lesson)]
    skip = {str(lesson.get("column") or "").lower() for lesson in active if lesson.get("kind") == "skip_column"}
    kept: list[dict[str, Any]] = []
    for check in checks:
        arguments = (check.get("check") or {}).get("arguments") or {}
        column = arguments.get("column")
        name = str(check.get("name") or "").lower()
        if isinstance(column, str) and column.lower() in skip:
            continue
        if any(name.startswith(f"{column}_") for column in skip):
            continue
        kept.append(check)
    names = {check["name"] for check in kept}
    for lesson in active:
        column = str(lesson.get("column") or "")
        if not column.isidentifier():
            continue
        kind = lesson.get("kind")
        if kind == "not_null":
            name = f"{column}_not_null"
            if name not in names:
                kept.append(
                    {
                        "name": name,
                        "criticality": "error",
                        "check": {"function": "is_not_null", "arguments": {"column": column}},
                    }
                )
                names.add(name)
        elif kind == "json":
            name = f"{column}_valid_json"
            if name not in names:
                ident = f"`{column}`"
                kept.append(
                    {
                        "name": name,
                        "criticality": "warn",
                        "check": {
                            "function": "sql_expression",
                            "arguments": {
                                "expression": (
                                    f"{ident} IS NULL OR TRIM(CAST({ident} AS STRING)) = '' "
                                    f"OR lower(TRIM(CAST({ident} AS STRING))) = 'null' "
                                    f"OR try_parse_json(CAST({ident} AS STRING)) IS NOT NULL"
                                ),
                                "msg": f"{column} should be valid JSON, blank, or null",
                                "negate": False,
                            },
                        },
                    }
                )
                names.add(name)
        elif kind == "format":
            from app.databricks.dqx_utils import format_regex

            rule = str(lesson.get("detail") or "").strip()
            name = f"{column}_learned_format"
            if not rule or name in names:
                continue
            try:
                pattern = format_regex(rule)
            except ValueError:
                continue
            escaped = pattern.replace("\\", "\\\\").replace("'", "\\'")
            ident = f"`{column}`"
            kept.append(
                {
                    "name": name,
                    "criticality": "error",
                    "check": {
                        "function": "sql_expression",
                        "arguments": {
                            "expression": (
                                f"{ident} IS NULL OR TRIM(CAST({ident} AS STRING)) = '' "
                                f"OR CAST({ident} AS STRING) RLIKE '{escaped}'"
                            ),
                            "msg": f"{column} must match {rule}",
                            "negate": False,
                        },
                    },
                }
            )
            names.add(name)
        elif kind == "allowed_values":
            values = [part.strip() for part in str(lesson.get("detail") or "").split(",") if part.strip()]
            if not values or len(values) > 20:
                continue
            name = f"{column}_allowed_values"
            if name in names:
                continue
            quoted = ", ".join(json.dumps(value) for value in values)
            ident = f"`{column}`"
            kept.append(
                {
                    "name": name,
                    "criticality": "error",
                    "check": {
                        "function": "sql_expression",
                        "arguments": {
                            "expression": f"{ident} IS NULL OR CAST({ident} AS STRING) IN ({quoted})",
                            "msg": f"{column} must be one of the learned values",
                            "negate": False,
                        },
                    },
                }
            )
            names.add(name)
    return kept
