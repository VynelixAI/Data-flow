"""Build DQX check definitions. Execution happens inside Databricks, not in the API process."""

from __future__ import annotations

import json
import re
from typing import Any

from app.databricks.identifiers import quote_ident, require_ident

FORMAT_PRESETS: dict[str, str] = {
    "email": r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$",
    "iso_date": r"^\d{4}-\d{2}-\d{2}$",
    "iso_timestamp": r"^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}",
    "uuid": r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$",
    "integer": r"^-?\d+$",
    "non_empty": r"^.+$",
}


def _check(name: str, function: str, arguments: dict[str, Any], *, criticality: str = "error") -> dict[str, Any]:
    return {
        "name": name,
        "criticality": criticality,
        "check": {"function": function, "arguments": arguments},
    }


def format_regex(rule: str) -> str:
    if rule.startswith("regex:"):
        pattern = rule[len("regex:") :]
    elif rule in FORMAT_PRESETS:
        pattern = FORMAT_PRESETS[rule]
    else:
        raise ValueError(
            f"Unknown format '{rule}'. Use one of {', '.join(sorted(FORMAT_PRESETS))} or regex:<pattern>."
        )
    if len(pattern) > 300 or any(token in pattern for token in ["'", '"', ";", "--", "/*", "*/", "\n"]):
        raise ValueError("Format pattern contains unsupported characters.")
    return pattern


# Spark date patterns. MM is the month; the session default is not reliable enough on its own.
_DATE_FORMATS = ("yyyy-MM-dd", "dd/MM/yyyy", "MM/dd/yyyy", "yyyy/MM/dd", "dd-MM-yyyy", "MM-dd-yyyy")


def parseable_date_expression(ident: str) -> str:
    """SQL that is true for null and for a date in one of the accepted formats.

    DQX ``sql_expression`` fails the row when this expression is false.
    Leave ``negate`` unset so it stays at that default.
    """
    value = f"trim(CAST({ident} AS STRING))"
    clauses = [
        f"{ident} IS NULL",
        f"try_to_timestamp({value}) IS NOT NULL",
    ]
    clauses.extend(f"try_to_date({value}, '{fmt}') IS NOT NULL" for fmt in _DATE_FORMATS)
    return " OR ".join(clauses)


def sql_format_predicate(column: str, rule: str) -> str:
    """True when the value is present and does not match the format."""
    ident = quote_ident(require_ident(column))
    if rule == "json":
        return (
            f"{ident} IS NOT NULL AND TRIM(CAST({ident} AS STRING)) != '' "
            f"AND try_parse_json(CAST({ident} AS STRING)) IS NULL"
        )
    pattern = format_regex(rule)
    escaped = pattern.replace("\\", "\\\\").replace("'", "\\'")
    return (
        f"{ident} IS NOT NULL AND TRIM(CAST({ident} AS STRING)) != '' "
        f"AND NOT (CAST({ident} AS STRING) RLIKE '{escaped}')"
    )


def build_dqx_checks(
    *,
    primary_keys: list[str],
    not_null_columns: list[str],
    formats: dict[str, str],
    json_columns: list[str],
    compare_columns: list[str] | None = None,
    columns: list[str] | None = None,
    column_types: dict[str, str] | None = None,
    samples: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    for column in primary_keys:
        checks.append(
            _check(
                f"{column}_not_null",
                "is_not_null",
                {"column": require_ident(column)},
            )
        )
    if primary_keys:
        checks.append(
            _check(
                "primary_key_is_unique",
                "is_unique",
                {"columns": [require_ident(column) for column in primary_keys]},
            )
        )
    seen = set(primary_keys)
    for column in not_null_columns:
        if column in seen:
            continue
        seen.add(column)
        checks.append(_check(f"{column}_not_null", "is_not_null", {"column": require_ident(column)}))

    for column, rule in formats.items():
        predicate = sql_format_predicate(column, rule)
        checks.append(
            _check(
                f"{column}_format_{rule.split(':', 1)[0][:24]}",
                "sql_expression",
                {
                    "expression": f"NOT ({predicate})",
                    "msg": f"{column} failed format rule {rule}",
                },
            )
        )

    for column in json_columns:
        predicate = sql_format_predicate(column, "json")
        checks.append(
            _check(
                f"{column}_json_parse",
                "sql_expression",
                {
                    "expression": f"NOT ({predicate})",
                    "msg": f"{column} is not valid JSON",
                },
            )
        )

    if compare_columns:
        # Touch the list so callers can pass the mapped grain without unused-arg noise.
        _ = [require_ident(column) for column in compare_columns]
    if columns:
        existing = {check["name"] for check in checks}
        for check in infer_quality_checks(columns, types=column_types, samples=samples):
            if check["name"] not in existing:
                checks.append(check)
                existing.add(check["name"])
    return checks


_MONEY = ("amount", "price", "cost", "total", "revenue", "balance", "fee")
_QUANTITY = ("qty", "quantity", "count", "units")
_JSON_NAMES = {"payload", "extra_info", "metadata", "attributes"}
_CATEGORY_NAMES = {"status", "state", "category"}
_NUMERIC_TYPES = ("int", "bigint", "integer", "long", "short", "double", "float", "decimal")
_DATE_TYPES = ("date", "timestamp")


def _safe_names(columns: list[str]) -> dict[str, str]:
    present: dict[str, str] = {}
    for name in columns:
        if isinstance(name, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            present.setdefault(name.lower(), name)
    return present


def _type_of(name: str, types: dict[str, str] | None) -> str:
    if not types:
        return ""
    for key, value in types.items():
        if key.lower() == name.lower():
            return str(value or "").lower()
    return ""


def _is_numeric_type(data_type: str) -> bool:
    return data_type.startswith(_NUMERIC_TYPES)


def _is_date_type(data_type: str) -> bool:
    return data_type.startswith(_DATE_TYPES)


def _samples_for(name: str, samples: dict[str, list[str]] | None) -> list[str]:
    if not samples:
        return []
    for key, values in samples.items():
        if key.lower() == name.lower():
            return [str(value) for value in values if value is not None and str(value).strip()]
    return []


def infer_quality_checks(
    columns: list[str],
    *,
    types: dict[str, str] | None = None,
    samples: dict[str, list[str]] | None = None,
) -> list[dict[str, Any]]:
    """Build DQX rules from this source's column names, types, and sample values.

    Nothing here is tied to one dataset. A later source with different columns
    gets a different rule set.
    """
    present = _safe_names(columns)
    checks: list[dict[str, Any]] = []

    def add(name: str, function: str, arguments: dict[str, Any], criticality: str = "error") -> None:
        checks.append(_check(name, function, arguments, criticality=criticality))

    for lower, original in present.items():
        data_type = _type_of(original, types)
        values = _samples_for(original, samples)
        ident = quote_ident(original)
        if lower == "id" or lower.endswith("_id"):
            add(f"{original}_not_null", "is_not_null", {"column": original})
        if "email" in lower:
            add(f"{original}_not_null", "is_not_null", {"column": original}, criticality="warn")
            add(
                f"{original}_valid_format",
                "sql_expression",
                {
                    "expression": (
                        f"{ident} IS NULL OR CAST({ident} AS STRING) RLIKE "
                        "'^[A-Za-z0-9._%+\\\\-]+@[A-Za-z0-9.\\\\-]+\\\\.[A-Za-z]{2,}$'"
                    ),
                    "msg": f"{original} must look like user@domain.tld",
                },
            )
        looks_like_date = "date" in lower or lower.endswith(("_at", "_ts", "_dt"))
        if looks_like_date and not _is_date_type(data_type):
            add(
                f"{original}_is_valid_date",
                "sql_expression",
                {
                    "expression": parseable_date_expression(ident),
                    "msg": f"{original} must be a parseable date",
                },
            )
        if any(token == lower or lower.endswith("_" + token) for token in _MONEY):
            if _is_numeric_type(data_type):
                add(
                    f"{original}_greater_than_zero",
                    "sql_expression",
                    {
                        "expression": f"{ident} IS NULL OR {ident} > 0",
                        "msg": f"{original} must be greater than zero",
                    },
                )
            else:
                numeric = "^[0-9]+(\\\\.[0-9]+)?$"
                add(
                    f"{original}_is_numeric",
                    "sql_expression",
                    {
                        "expression": f"{ident} IS NULL OR CAST({ident} AS STRING) RLIKE '{numeric}'",
                        "msg": f"{original} must be numeric",
                    },
                )
                add(
                    f"{original}_greater_than_zero",
                    "sql_expression",
                    {
                        "expression": (
                            f"{ident} IS NULL OR NOT (CAST({ident} AS STRING) RLIKE '{numeric}') "
                            f"OR CAST({ident} AS DECIMAL(18,2)) > 0"
                        ),
                        "msg": f"{original} must be greater than zero",
                    },
                )
        if any(token == lower or lower.endswith("_" + token) for token in _QUANTITY) and not _is_numeric_type(data_type):
            numeric = "^-?[0-9]+(\\\\.[0-9]+)?$"
            add(
                f"{original}_is_numeric",
                "sql_expression",
                {
                    "expression": f"{ident} IS NULL OR CAST({ident} AS STRING) RLIKE '{numeric}'",
                    "msg": f"{original} must be numeric",
                },
            )
        json_samples = any(value.lstrip()[:1] in "{[" for value in values)
        if "json" in lower or lower in _JSON_NAMES or json_samples:
            add(
                f"{original}_valid_json",
                "sql_expression",
                {
                    "expression": (
                        f"{ident} IS NULL OR TRIM(CAST({ident} AS STRING)) = '' "
                        f"OR lower(TRIM(CAST({ident} AS STRING))) = 'null' "
                        f"OR try_parse_json(CAST({ident} AS STRING)) IS NOT NULL"
                    ),
                    "msg": f"{original} should be valid JSON, blank, or null",
                },
                criticality="warn",
            )
        if lower in _CATEGORY_NAMES:
            add(f"{original}_not_null", "is_not_null", {"column": original})
            spellings: dict[str, list[str]] = {}
            for value in values:
                token = value.strip()
                if token and token.lower() != "null":
                    spellings.setdefault(token.lower(), []).append(token)
            mixed = {
                token: max(set(group), key=group.count)
                for token, group in spellings.items()
                if len(set(group)) > 1
            }
            if mixed and len(spellings) <= 12:
                allowed = ", ".join(json.dumps(token) for token in sorted(mixed))
                canonical = ", ".join(json.dumps(mixed[token]) for token in sorted(mixed))
                add(
                    f"{original}_case_inconsistent",
                    "sql_expression",
                    {
                        "expression": (
                            f"{ident} IS NULL OR lower(CAST({ident} AS STRING)) NOT IN ({allowed}) "
                            f"OR CAST({ident} AS STRING) IN ({canonical})"
                        ),
                        "msg": f"{original} casing is inconsistent with the other values in this source",
                    },
                    criticality="warn",
                )
    return checks


def dqx_checks_yaml(checks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for check in checks:
        lines.append(f"- name: {json.dumps(check['name'])}")
        lines.append(f"  criticality: {check['criticality']}")
        lines.append("  check:")
        lines.append(f"    function: {check['check']['function']}")
        lines.append("    arguments:")
        for key, value in check["check"]["arguments"].items():
            if isinstance(value, list):
                lines.append(f"      {key}:")
                for item in value:
                    lines.append(f"        - {json.dumps(item)}")
            else:
                rendered = json.dumps(value) if isinstance(value, str) else json.dumps(value)
                lines.append(f"      {key}: {rendered}")
    return "\n".join(lines) + ("\n" if lines else "")


def suggest_repairs(profile: dict[str, Any]) -> list[dict[str, str]]:
    """Turn a column profile into conservative repair suggestions. Nothing is applied automatically."""
    suggestions: list[dict[str, str]] = []
    for column in profile.get("columns", []):
        name = column.get("name")
        if not name or not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        samples = [str(value) for value in column.get("samples") or [] if value is not None]
        nulls = int(column.get("null_count") or 0)
        if any(sample[:1] in "{[" for sample in samples):
            suggestions.append(
                {
                    "column": name,
                    "action": "parse_json",
                    "reason": "Sample values look serialized (JSON).",
                }
            )
        if any(sample != sample.strip() for sample in samples if isinstance(sample, str)):
            suggestions.append({"column": name, "action": "trim", "reason": "Sample values have surrounding whitespace."})
        if samples and all(sample.lower() in {"y", "n", "yes", "no", "true", "false", "0", "1"} for sample in samples):
            suggestions.append(
                {"column": name, "action": "standardize_boolean", "reason": "Values look like mixed boolean encodings."}
            )
        if nulls and column.get("empty_string_count"):
            suggestions.append(
                {"column": name, "action": "empty_to_null", "reason": "Empty strings should be null before quality checks."}
            )
    return suggestions
