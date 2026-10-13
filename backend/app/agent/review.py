"""Check a copy plan before any rows are written."""

from __future__ import annotations

from typing import Any

from app.schemas import ColumnSpec, PipelineSpec


def review_copy(
    spec: PipelineSpec,
    source_columns: list[ColumnSpec],
    target_columns: list[ColumnSpec],
    checks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Validate paths, keys, and shared columns. Notes are shown with the notebook."""
    blockers: list[str] = []
    notes: list[str] = []
    source_names = {column.name.lower(): column.name for column in source_columns}
    target_names = {column.name.lower(): column.name for column in target_columns}
    shared = [source_names[name] for name in source_names if name in target_names]

    if spec.source_table.strip() == spec.target_table.strip():
        blockers.append("The source and destination are the same object, so the copy would overwrite itself.")
    if not spec.primary_keys:
        blockers.append("A primary key is required before the copy can run.")
    for key in spec.primary_keys:
        if key.lower() not in source_names:
            blockers.append(f"Primary key {key} is not a column on the source.")
        elif target_columns and key.lower() not in target_names:
            blockers.append(f"Primary key {key} is not a column on the destination.")
    if spec.compare_columns:
        shared_lower = {name.lower() for name in shared}
        for column in spec.compare_columns:
            if column.lower() not in shared_lower:
                blockers.append(f"{column} is not on both sides, so it cannot be validated.")
        notes.append("Validation is limited to: " + ", ".join(spec.compare_columns) + ".")
    elif shared:
        notes.append("Every shared column will be validated: " + ", ".join(shared) + ".")
    else:
        blockers.append("The source and destination have no columns in common.")

    for check in checks:
        arguments = ((check.get("check") or {}).get("arguments") or {})
        if "negate" in arguments:
            blockers.append(
                f"{check.get('name') or 'A quality check'} sets negate. "
                "The SQL expression must stay the pass condition."
            )
    if spec.primary_keys:
        notes.append("Primary key: " + ", ".join(spec.primary_keys) + ".")
    notes.append(f"Write mode is {spec.write_mode}. Rows are not copied until you run this notebook.")
    if checks:
        names = [str(check.get("name")) for check in checks if check.get("name")]
        preview = ", ".join(names[:8])
        extra = f" and {len(names) - 8} more" if len(names) > 8 else ""
        notes.append(f"{len(checks)} quality checks are in the notebook: {preview}{extra}.")
    return {"ok": not blockers, "blockers": blockers, "notes": notes, "shared_columns": shared}
