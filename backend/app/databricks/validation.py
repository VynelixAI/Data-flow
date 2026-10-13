"""Source-to-target validation at the latest published snapshot.

The comparison grain is the primary key (composite keys supported). Each side is
restricted to one ``domain_published_ts`` value: its own maximum, a shared
maximum, or an explicit published timestamp. Duplicate keys are counted and then
collapsed so a FULL OUTER JOIN cannot multiply mismatches.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.databricks.dqx_utils import build_dqx_checks, sql_format_predicate
from app.databricks.identifiers import (
    parse_uc_location,
    quote_ident,
    require_ident,
    sql_float,
    sql_relation,
    sql_string_literal,
    uc_namespace,
    validate_spark_type,
    validate_timestamp_literal,
)
from app.errors import DataflowError
from app.schemas import (
    AggregateMismatch,
    ColumnDiff,
    ColumnSpec,
    DuplicateKeySample,
    FormatCheck,
    MismatchRow,
    NullCheck,
    SchemaDrift,
    SqlStep,
    ValidationOutcome,
    ValidationReport,
    ValidationRequest,
)

_MAX_COMPARE_COLUMNS = 80


def type_family(data_type: str) -> str:
    base = (data_type or "string").lower().strip().split("<", 1)[0].split("(", 1)[0].strip()
    if base in {"byte", "tinyint", "short", "smallint", "int", "integer", "long", "bigint"}:
        return "integral"
    if base in {"float", "double", "decimal", "numeric"}:
        return "fractional"
    if base == "date":
        return "date"
    if base in {"timestamp", "timestamp_ntz", "timestamp_ltz"}:
        return "timestamp"
    if base in {"bool", "boolean"}:
        return "boolean"
    if base in {"string", "varchar", "char"}:
        return "string"
    if base == "binary":
        return "binary"
    return "complex"


def _index(columns: list[ColumnSpec]) -> dict[str, ColumnSpec]:
    indexed: dict[str, ColumnSpec] = {}
    for column in columns:
        require_ident(column.name)
        indexed[column.name.lower()] = column
    return indexed


def _unused_name(source: list[ColumnSpec], target: list[ColumnSpec], base: str) -> str:
    taken = {column.name.lower() for column in [*source, *target]}
    name = base
    suffix = 2
    while name.lower() in taken:
        name = f"{base}_{suffix}"
        suffix += 1
    return name


def compute_schema_drift(
    source: list[ColumnSpec],
    target: list[ColumnSpec],
    *,
    compared: list[str],
    primary_keys: list[str],
    timestamp_column: str,
) -> SchemaDrift:
    source_idx = _index(source)
    target_idx = _index(target)
    only_source = sorted(source_idx[name].name for name in source_idx if name not in target_idx)
    only_target = sorted(target_idx[name].name for name in target_idx if name not in source_idx)
    focus = {name.lower() for name in [*compared, *primary_keys, timestamp_column]}
    mismatches: list[dict[str, str]] = []
    warnings: list[str] = []
    for name, source_col in source_idx.items():
        target_col = target_idx.get(name)
        if target_col is None:
            continue
        if source_col.data_type.lower() == target_col.data_type.lower():
            continue
        severity = "error" if type_family(source_col.data_type) != type_family(target_col.data_type) else "warning"
        entry = {
            "column": source_col.name,
            "source_type": source_col.data_type,
            "target_type": target_col.data_type,
            "severity": severity,
        }
        mismatches.append(entry)
        if severity == "warning":
            warnings.append(
                f"{source_col.name} type differs within the same family "
                f"({source_col.data_type} vs {target_col.data_type})."
            )
        elif name in focus:
            warnings.append(
                f"{source_col.name} datatype family differs ({source_col.data_type} vs {target_col.data_type})."
            )
    if only_source:
        warnings.append("Columns only in source: " + ", ".join(only_source))
    if only_target:
        warnings.append("Columns only in target: " + ", ".join(only_target))
    return SchemaDrift(
        only_in_source=only_source,
        only_in_target=only_target,
        type_mismatches=mismatches,
        warnings=warnings,
    )


def _require_column(columns: list[ColumnSpec], name: str, *, table: str) -> ColumnSpec:
    found = _index(columns).get(name.lower())
    if found is None:
        raise DataflowError(
            f"Column '{name}' was not found on {table}.",
            code="missing_column",
        )
    return found


def resolve_compare_pairs(
    request: ValidationRequest,
    source: list[ColumnSpec],
    target: list[ColumnSpec],
) -> tuple[list[tuple[ColumnSpec, ColumnSpec]], list[str]]:
    source_idx = _index(source)
    target_idx = _index(target)
    excluded = {request.timestamp_column.lower(), *(key.lower() for key in request.primary_keys)}
    excluded.update(column.lower() for column in request.exclude_columns)
    if request.soft_delete_column:
        excluded.add(request.soft_delete_column.lower())

    warnings: list[str] = []
    requested = request.compare_columns or None
    if requested is None:
        names = [
            column.name
            for column in source
            if column.name.lower() not in excluded
            and not column.name.lower().startswith("_dataflow")
            and column.name.lower() in target_idx
        ]
    else:
        names = []
        for name in requested:
            require_ident(name)
            if name.lower() not in source_idx or name.lower() not in target_idx:
                warnings.append(f"Skipped compare column '{name}' because it is not on both tables.")
                continue
            names.append(name)

    pairs: list[tuple[ColumnSpec, ColumnSpec]] = []
    for name in names:
        if name.lower() in excluded:
            continue
        pairs.append((source_idx[name.lower()], target_idx[name.lower()]))
    if len(pairs) > _MAX_COMPARE_COLUMNS:
        warnings.append(
            f"Comparing the first {_MAX_COMPARE_COLUMNS} shared columns. Narrow compare_columns to check the rest."
        )
        pairs = pairs[:_MAX_COMPARE_COLUMNS]
    if not pairs:
        warnings.append("No mapped attributes to compare. Validation covers keys, counts, duplicates, and null primary keys.")
    return pairs, warnings


def _values_differ(source_sql: str, target_sql: str, source_type: str, target_type: str, request: ValidationRequest) -> str:
    source_family = type_family(source_type)
    target_family = type_family(target_type)
    both_null = f"{source_sql} IS NULL AND {target_sql} IS NULL"
    either_null = f"{source_sql} IS NULL OR {target_sql} IS NULL"
    if request.null_equivalence:
        null_clause = f"WHEN {both_null} THEN 0 WHEN {either_null} THEN 1"
    else:
        null_clause = f"WHEN {either_null} THEN 1"

    numeric = {"integral", "fractional"}
    if source_family in numeric and target_family in numeric:
        compare = (
            f"ABS(CAST({source_sql} AS DOUBLE) - CAST({target_sql} AS DOUBLE)) > {sql_float(request.float_tolerance)}"
        )
    elif source_family == target_family and source_family in {"timestamp", "date"}:
        compare = f"NOT (CAST({source_sql} AS TIMESTAMP) <=> CAST({target_sql} AS TIMESTAMP))"
    elif source_family == target_family == "boolean":
        compare = f"NOT ({source_sql} <=> {target_sql})"
    else:
        # String compare absorbs serialization differences such as padded varchars.
        # NULL <=> NULL is true, so null equivalence still holds when null_equivalence is on.
        compare = f"NOT (TRIM(CAST({source_sql} AS STRING)) <=> TRIM(CAST({target_sql} AS STRING)))"
    return f"CASE {null_clause} WHEN {compare} THEN 1 ELSE 0 END"


def _soft_delete(alias: str, column: str | None) -> str:
    if not column:
        return ""
    ident = f"{alias}.{quote_ident(column)}"
    return (
        f" AND COALESCE(LOWER(CAST({ident} AS STRING)) IN ('true', '1', 'yes', 'y'), false) = false"
    )


def _pk_not_null(alias: str, keys: list[str]) -> str:
    return " AND ".join(f"{alias}.{quote_ident(key)} IS NOT NULL" for key in keys)


def _pk_list(alias: str, keys: list[str]) -> str:
    return ", ".join(f"{alias}.{quote_ident(key)}" for key in keys)


def _timestamp_expr(alias: str, request: ValidationRequest, *, side: str) -> str:
    pinned = request.source_published_ts if side == "source" else request.target_published_ts
    column = f"{alias}.{quote_ident(request.timestamp_column)}"
    if pinned:
        literal = sql_string_literal(validate_timestamp_literal(pinned))
        return f"{column} = TIMESTAMP {literal}"
    if request.timestamp_alignment == "common_max":
        return f"{column} = c.common_ts"
    max_column = "c.source_max_ts" if side == "source" else "c.target_max_ts"
    return f"{column} = {max_column}"


def build_validation_plan(
    request: ValidationRequest,
    source_columns: list[ColumnSpec],
    target_columns: list[ColumnSpec],
) -> tuple[list[SqlStep], SchemaDrift, list[str], list[tuple[ColumnSpec, ColumnSpec]]]:
    for key in request.primary_keys:
        require_ident(key)
    require_ident(request.timestamp_column)
    if request.soft_delete_column:
        require_ident(request.soft_delete_column)
    for column in [*request.not_null_columns, *request.json_columns, *request.aggregate_dimensions, *request.aggregate_measures, *request.exclude_columns]:
        require_ident(column)

    source_table = sql_relation(request.source_table)
    target_table = sql_relation(request.target_table)
    for column in [*source_columns, *target_columns]:
        validate_spark_type(column.data_type)
    for key in request.primary_keys:
        _require_column(source_columns, key, table=request.source_table)
        _require_column(target_columns, key, table=request.target_table)
    has_snapshot = (
        request.timestamp_column.lower() in _index(source_columns)
        and request.timestamp_column.lower() in _index(target_columns)
    )
    if request.source_published_ts or request.target_published_ts or has_snapshot:
        _require_column(source_columns, request.timestamp_column, table=request.source_table)
        _require_column(target_columns, request.timestamp_column, table=request.target_table)
        has_snapshot = True
    source_soft = None
    target_soft = None
    if request.soft_delete_column:
        source_names = _index(source_columns)
        target_names = _index(target_columns)
        if request.soft_delete_column.lower() in source_names:
            source_soft = source_names[request.soft_delete_column.lower()].name
        if request.soft_delete_column.lower() in target_names:
            target_soft = target_names[request.soft_delete_column.lower()].name
        if source_soft is None and target_soft is None:
            raise DataflowError(
                f"Soft-delete column '{request.soft_delete_column}' was not found on either table.",
                code="missing_column",
            )

    pairs, warnings = resolve_compare_pairs(request, source_columns, target_columns)
    if not has_snapshot:
        warnings.append(
            f"{request.timestamp_column} is not on both sides, so validation compares the full source and destination."
        )
    if request.soft_delete_column and bool(source_soft) != bool(target_soft):
        present_on = "source" if source_soft else "target"
        warnings.append(
            f"Soft-delete column '{request.soft_delete_column}' exists only on the {present_on} table. "
            "The other table is not filtered."
        )
    compared_names = [pair[0].name for pair in pairs]
    drift = compute_schema_drift(
        source_columns,
        target_columns,
        compared=compared_names,
        primary_keys=request.primary_keys,
        timestamp_column=request.timestamp_column,
    )
    warnings.extend(drift.warnings)

    present = _unused_name(source_columns, target_columns, "_dataflow_present")
    row_number = _unused_name(source_columns, target_columns, "_dataflow_rn")
    present_sql = quote_ident(present)
    rn_sql = quote_ident(row_number)
    ts = quote_ident(request.timestamp_column)

    order_cols = [quote_ident(column.name) for column, _ in pairs] or [quote_ident(key) for key in request.primary_keys]
    order_struct = ", ".join(f"'c{index}', {column}" for index, column in enumerate(order_cols))

    def latest(alias_table: str, table_sql: str, side: str, soft_column: str | None) -> str:
        predicate = _timestamp_expr("src", request, side=side) if has_snapshot else "1 = 1"
        join = "\n  CROSS JOIN chosen c" if has_snapshot else ""
        return f"""{alias_table} AS (
  SELECT src.*, 1 AS {present_sql}
  FROM {table_sql} src{join}
  WHERE {predicate}{_soft_delete("src", soft_column)}
)"""

    grain_order = f"to_json(named_struct({order_struct}))"
    source_keys = _pk_list("src", request.primary_keys)
    if has_snapshot:
        bounds_sql = f"""WITH bounds AS (
  SELECT
    (SELECT MAX({ts}) FROM {source_table}) AS source_max_ts,
    (SELECT MAX({ts}) FROM {target_table}) AS target_max_ts
),
chosen AS (
  SELECT source_max_ts, target_max_ts, LEAST(source_max_ts, target_max_ts) AS common_ts
  FROM bounds
),"""
    else:
        bounds_sql = """WITH bounds AS (
  SELECT CAST(NULL AS TIMESTAMP) AS source_max_ts, CAST(NULL AS TIMESTAMP) AS target_max_ts
),
chosen AS (
  SELECT source_max_ts, target_max_ts, CAST(NULL AS TIMESTAMP) AS common_ts
  FROM bounds
),"""
    ctes = f"""{bounds_sql}
{latest("source_latest", source_table, "source", source_soft)},
{latest("target_latest", target_table, "target", target_soft)},
source_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY {source_keys} ORDER BY {grain_order}) AS {rn_sql}
  FROM source_latest src
  WHERE {_pk_not_null("src", request.primary_keys)}
),
target_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY {_pk_list("src", request.primary_keys)} ORDER BY {grain_order}) AS {rn_sql}
  FROM target_latest src
  WHERE {_pk_not_null("src", request.primary_keys)}
),
source_grain AS (
  SELECT * FROM source_ranked WHERE {rn_sql} = 1
),
target_grain AS (
  SELECT * FROM target_ranked WHERE {rn_sql} = 1
)"""

    differ_exprs = [
        _values_differ(f"s.{quote_ident(source.name)}", f"t.{quote_ident(target.name)}", source.data_type, target.data_type, request)
        for source, target in pairs
    ]
    either_differs = " OR ".join(f"({expr}) = 1" for expr in differ_exprs) if differ_exprs else "1 = 0"
    # Missing keys are counted on their own. A null side must not also look like a value mismatch.
    value_predicate = f"s.{present_sql} IS NOT NULL AND t.{present_sql} IS NOT NULL AND ({either_differs})"
    value_flag = f"CASE WHEN {value_predicate} THEN 1 ELSE 0 END"

    join = " AND ".join(
        f"s.{quote_ident(key)} = t.{quote_ident(key)}" for key in request.primary_keys
    )
    pk_struct = ", ".join(
        f"'{key}', CAST(COALESCE(s.{quote_ident(key)}, t.{quote_ident(key)}) AS STRING)"
        for key in request.primary_keys
    )
    diff_structs = []
    for (source, target), expr in zip(pairs, differ_exprs, strict=True):
        diff_structs.append(
            "named_struct("
            f"'column', '{source.name}', "
            f"'source_value', CAST(s.{quote_ident(source.name)} AS STRING), "
            f"'target_value', CAST(t.{quote_ident(target.name)} AS STRING), "
            f"'differs', {expr})"
        )
    diffs_sql = (
        f"filter(array({', '.join(diff_structs)}), item -> item.differs = 1)"
        if diff_structs
        else "array()"
    )

    grouped_source = (
        f"(SELECT COUNT(*) AS c FROM source_latest src WHERE {_pk_not_null('src', request.primary_keys)} "
        f"GROUP BY {_pk_list('src', request.primary_keys)})"
    )
    grouped_target = (
        f"(SELECT COUNT(*) AS c FROM target_latest src WHERE {_pk_not_null('src', request.primary_keys)} "
        f"GROUP BY {_pk_list('src', request.primary_keys)})"
    )

    def used_ts(side: str) -> str:
        if not has_snapshot:
            return "CAST(NULL AS STRING)"
        pinned = request.source_published_ts if side == "source" else request.target_published_ts
        if pinned:
            literal = sql_string_literal(validate_timestamp_literal(pinned))
            return f"CAST(TIMESTAMP {literal} AS STRING)"
        if request.timestamp_alignment == "common_max":
            return "CAST(common_ts AS STRING)"
        return "CAST(source_max_ts AS STRING)" if side == "source" else "CAST(target_max_ts AS STRING)"

    metrics_sql = f"""{ctes}
SELECT
  (SELECT {used_ts("source")} FROM chosen) AS source_used_ts,
  (SELECT {used_ts("target")} FROM chosen) AS target_used_ts,
  (SELECT CAST(source_max_ts AS STRING) FROM chosen) AS source_max_ts,
  (SELECT CAST(target_max_ts AS STRING) FROM chosen) AS target_max_ts,
  (SELECT COUNT(*) FROM source_latest) AS source_count,
  (SELECT COUNT(*) FROM target_latest) AS target_count,
  (SELECT COUNT(*) FROM source_latest src WHERE NOT ({_pk_not_null("src", request.primary_keys)})) AS null_pk_source,
  (SELECT COUNT(*) FROM target_latest src WHERE NOT ({_pk_not_null("src", request.primary_keys)})) AS null_pk_target,
  (SELECT COALESCE(SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END), 0) FROM {grouped_source} duplicates) AS duplicate_keys_source,
  (SELECT COALESCE(SUM(CASE WHEN c > 1 THEN c ELSE 0 END), 0) FROM {grouped_source} duplicates) AS duplicate_rows_source,
  (SELECT COALESCE(SUM(CASE WHEN c > 1 THEN 1 ELSE 0 END), 0) FROM {grouped_target} duplicates) AS duplicate_keys_target,
  (SELECT COALESCE(SUM(CASE WHEN c > 1 THEN c ELSE 0 END), 0) FROM {grouped_target} duplicates) AS duplicate_rows_target,
  (SELECT COALESCE(SUM(CASE WHEN s.{present_sql} IS NULL THEN 1 ELSE 0 END), 0)
     FROM source_grain s FULL OUTER JOIN target_grain t ON {join}) AS missing_in_source,
  (SELECT COALESCE(SUM(CASE WHEN t.{present_sql} IS NULL THEN 1 ELSE 0 END), 0)
     FROM source_grain s FULL OUTER JOIN target_grain t ON {join}) AS missing_in_target,
  (SELECT COALESCE(SUM({value_flag}), 0)
     FROM source_grain s FULL OUTER JOIN target_grain t ON {join}) AS value_mismatch
"""

    samples_sql = f"""{ctes}
SELECT
  CASE
    WHEN s.{present_sql} IS NULL THEN 'MISSING_IN_SOURCE'
    WHEN t.{present_sql} IS NULL THEN 'MISSING_IN_TARGET'
    ELSE 'VALUE_MISMATCH'
  END AS mismatch_type,
  to_json(named_struct({pk_struct})) AS primary_key_json,
  to_json({diffs_sql}) AS column_diffs_json
FROM source_grain s
FULL OUTER JOIN target_grain t ON {join}
WHERE s.{present_sql} IS NULL OR t.{present_sql} IS NULL OR ({value_predicate})
LIMIT {int(request.max_mismatch_rows)}
"""

    def duplicate_sql(latest_name: str) -> str:
        keys = ", ".join(f"g.{quote_ident(key)}" for key in request.primary_keys)
        return f"""{ctes}
SELECT {keys}, COUNT(*) AS occurrences
FROM {latest_name} g
WHERE {_pk_not_null("g", request.primary_keys)}
GROUP BY {keys}
HAVING COUNT(*) > 1
LIMIT 20
"""

    steps = [
        SqlStep(name="metrics", sql=metrics_sql.strip() + "\n", critical=True),
        SqlStep(name="mismatch_samples", sql=samples_sql.strip() + "\n", critical=False),
        SqlStep(name="duplicate_samples_source", sql=duplicate_sql("source_latest").strip() + "\n", critical=False),
        SqlStep(name="duplicate_samples_target", sql=duplicate_sql("target_latest").strip() + "\n", critical=False),
    ]

    null_columns = []
    seen: set[str] = set()
    for name in [*request.primary_keys, *request.not_null_columns, *compared_names]:
        if name.lower() in seen:
            continue
        seen.add(name.lower())
        null_columns.append(name)
    if null_columns:
        branches = []
        for side, latest in (("source", "source_latest"), ("target", "target_latest")):
            for name in null_columns:
                ident = quote_ident(_require_column(source_columns if side == "source" else target_columns, name, table=side).name)
                branches.append(
                    "SELECT "
                    f"'{side}' AS side, '{name}' AS column_name, "
                    f"SUM(CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END) AS null_count, "
                    f"COUNT(*) AS total FROM {latest}"
                )
        steps.append(SqlStep(name="null_profile", sql=f"{ctes}\n" + "\nUNION ALL\n".join(branches) + "\n", critical=False))

    format_branches = _quality_branches(ctes, request, source_columns, target_columns, pairs)
    if format_branches:
        steps.append(SqlStep(name="quality_checks", sql=format_branches, critical=False))

    if request.aggregate_dimensions and request.aggregate_measures:
        steps.append(SqlStep(name="aggregates", sql=_aggregate_sql(ctes, request, source_columns, target_columns), critical=False))
    elif request.aggregate_dimensions or request.aggregate_measures:
        warnings.append("Aggregate validation needs both aggregate_dimensions and aggregate_measures.")

    return steps, drift, warnings, pairs


def _quality_branches(
    ctes: str,
    request: ValidationRequest,
    source_columns: list[ColumnSpec],
    target_columns: list[ColumnSpec],
    pairs: list[tuple[ColumnSpec, ColumnSpec]],
) -> str:
    branches: list[str] = []
    rules: list[tuple[str, str]] = []
    for column, rule in request.formats.items():
        rules.append((column, rule))
    for column in request.json_columns:
        rules.append((column, "json"))
    for source, target in pairs:
        if type_family(source.data_type) != type_family(target.data_type):
            rules.append((source.name, f"cast:{target.data_type}"))

    for side, latest, columns in (
        ("source", "source_latest", source_columns),
        ("target", "target_latest", target_columns),
    ):
        indexed = _index(columns)
        for column, rule in rules:
            spec = indexed.get(column.lower())
            if spec is None:
                continue
            ident = quote_ident(spec.name)
            if rule.startswith("cast:"):
                family = type_family(rule.split(":", 1)[1])
                cast_target = "DOUBLE" if family in {"integral", "fractional"} else "STRING"
                if family in {"timestamp", "date"}:
                    cast_target = "TIMESTAMP"
                predicate = f"{ident} IS NOT NULL AND try_cast({ident} AS {cast_target}) IS NULL"
                label = "datatype_cast"
            else:
                try:
                    predicate = sql_format_predicate(spec.name, rule)
                except ValueError as exc:
                    raise DataflowError(str(exc), code="invalid_format") from exc
                label = rule.split(":", 1)[0]
            branches.append(
                "SELECT "
                f"'{side}' AS side, '{spec.name}' AS column_name, '{label}' AS rule, "
                f"SUM(CASE WHEN {predicate} THEN 1 ELSE 0 END) AS failure_count, "
                f"COUNT(*) AS total FROM {latest}"
            )
    if not branches:
        return ""
    return f"{ctes}\n" + "\nUNION ALL\n".join(branches) + "\n"


def _aggregate_sql(
    ctes: str,
    request: ValidationRequest,
    source_columns: list[ColumnSpec],
    target_columns: list[ColumnSpec],
) -> str:
    source_idx = _index(source_columns)
    target_idx = _index(target_columns)
    dims = []
    for name in request.aggregate_dimensions:
        if name.lower() not in source_idx or name.lower() not in target_idx:
            raise DataflowError(f"Aggregate dimension '{name}' must exist on both tables.", code="missing_column")
        dims.append(source_idx[name.lower()].name)
    measures = []
    for name in request.aggregate_measures:
        if name.lower() not in source_idx or name.lower() not in target_idx:
            raise DataflowError(f"Aggregate measure '{name}' must exist on both tables.", code="missing_column")
        measures.append(source_idx[name.lower()].name)

    dim_select = ", ".join(quote_ident(name) for name in dims)
    metric_bits = ["COUNT(*) AS metric_count"]
    for measure in measures:
        ident = quote_ident(measure)
        metric_bits.append(f"SUM(CAST({ident} AS DOUBLE)) AS sum__{measure}")
        metric_bits.append(f"MIN(CAST({ident} AS DOUBLE)) AS min__{measure}")
        metric_bits.append(f"MAX(CAST({ident} AS DOUBLE)) AS max__{measure}")
    metrics = ", ".join(metric_bits)

    def agg(name: str, latest: str) -> str:
        return f"{name} AS (SELECT {dim_select}, {metrics} FROM {latest} GROUP BY {dim_select})"

    join = " AND ".join(
        f"s.{quote_ident(name)} <=> t.{quote_ident(name)}" for name in dims
    )
    projected = []
    for name in dims:
        projected.append(
            f"CAST(COALESCE(s.{quote_ident(name)}, t.{quote_ident(name)}) AS STRING) AS dim__{name}"
        )
    projected.append("s.metric_count AS source__count")
    projected.append("t.metric_count AS target__count")
    filters = ["NOT (s.metric_count <=> t.metric_count)"]
    tolerance = sql_float(request.float_tolerance)
    for measure in measures:
        for metric in ("sum", "min", "max"):
            alias = f"{metric}__{measure}"
            projected.append(f"CAST(s.{alias} AS STRING) AS source__{alias}")
            projected.append(f"CAST(t.{alias} AS STRING) AS target__{alias}")
            filters.append(
                f"ABS(COALESCE(s.{alias}, 0) - COALESCE(t.{alias}, 0)) > {tolerance}"
            )
    return f"""{ctes},
{agg("source_agg", "source_latest")},
{agg("target_agg", "target_latest")}
SELECT {", ".join(projected)}
FROM source_agg s
FULL OUTER JOIN target_agg t ON {join}
WHERE {" OR ".join(filters)}
LIMIT 200
"""


def _metric(row: dict[str, Any], name: str) -> Any:
    wanted = name.lower()
    for key, value in row.items():
        if str(key).lower() == wanted:
            return value
    return None


def _as_int(value: Any) -> int:
    if value is None or value == "":
        return 0
    return int(float(value))


def _as_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text[:500]


def _parse_json(value: Any) -> Any:
    if value is None or isinstance(value, (dict, list)):
        return value
    if isinstance(value, str) and value:
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return None
    return None


def _numbers_differ(left: Any, right: Any, tolerance: float) -> bool:
    if left in (None, "") and right in (None, ""):
        return False
    if left in (None, "") or right in (None, ""):
        return True
    return abs(float(left) - float(right)) > tolerance


def assemble_report(
    request: ValidationRequest,
    *,
    results: dict[str, list[dict[str, Any]]],
    step_errors: dict[str, str],
    drift: SchemaDrift,
    warnings: list[str],
    dqx_checks: list[dict[str, Any]],
    duplicate_keys_collapsed: bool,
) -> ValidationReport:
    now = datetime.now(timezone.utc).isoformat()
    metrics = (results.get("metrics") or [{}])[0] if "metrics" in results else {}
    report = ValidationReport(
        status="ERROR" if step_errors.get("metrics") else "PASS",
        source_table=request.source_table,
        target_table=request.target_table,
        primary_keys=list(request.primary_keys),
        timestamp_column=request.timestamp_column,
        timestamp_alignment=request.timestamp_alignment,
        execution_ts=now,
        schema_drift=drift,
        warnings=list(warnings),
        step_errors=dict(step_errors),
        dqx_checks=dqx_checks,
        source_domain_published_ts=_as_str(_metric(metrics, "source_used_ts")),
        target_domain_published_ts=_as_str(_metric(metrics, "target_used_ts")),
        source_max_domain_published_ts=_as_str(_metric(metrics, "source_max_ts")),
        target_max_domain_published_ts=_as_str(_metric(metrics, "target_max_ts")),
        source_count=_as_int(_metric(metrics, "source_count")),
        target_count=_as_int(_metric(metrics, "target_count")),
        missing_in_target_count=_as_int(_metric(metrics, "missing_in_target")),
        missing_in_source_count=_as_int(_metric(metrics, "missing_in_source")),
        value_mismatch_count=_as_int(_metric(metrics, "value_mismatch")),
        duplicate_keys_source=_as_int(_metric(metrics, "duplicate_keys_source")),
        duplicate_keys_target=_as_int(_metric(metrics, "duplicate_keys_target")),
        duplicate_rows_source=_as_int(_metric(metrics, "duplicate_rows_source")),
        duplicate_rows_target=_as_int(_metric(metrics, "duplicate_rows_target")),
        null_pk_source=_as_int(_metric(metrics, "null_pk_source")),
        null_pk_target=_as_int(_metric(metrics, "null_pk_target")),
    )
    report.mismatch_count = (
        report.missing_in_target_count + report.missing_in_source_count + report.value_mismatch_count
    )

    for row in results.get("mismatch_samples") or []:
        raw_key = _parse_json(row.get("primary_key_json")) or {}
        raw_diffs = _parse_json(row.get("column_diffs_json")) or []
        mismatch_type = str(row.get("mismatch_type") or "VALUE_MISMATCH")
        if mismatch_type not in {"MISSING_IN_TARGET", "MISSING_IN_SOURCE", "VALUE_MISMATCH"}:
            mismatch_type = "VALUE_MISMATCH"
        diffs = []
        if mismatch_type == "VALUE_MISMATCH":
            for item in raw_diffs:
                if not isinstance(item, dict):
                    continue
                diffs.append(
                    ColumnDiff(
                        column=str(item.get("column")),
                        source_value=_as_str(item.get("source_value")),
                        target_value=_as_str(item.get("target_value")),
                    )
                )
        report.mismatched_keys.append(
            MismatchRow(
                primary_key={str(key): _as_str(value) for key, value in raw_key.items()},
                mismatch_type=mismatch_type,  # type: ignore[arg-type]
                column_diffs=diffs,
            )
        )
    report.quarantine_preview = list(report.mismatched_keys)

    for side, step in (("source", "duplicate_samples_source"), ("target", "duplicate_samples_target")):
        for row in results.get(step) or []:
            key = {
                column: _as_str(row.get(column))
                for column in request.primary_keys
            }
            report.duplicate_key_samples.append(
                DuplicateKeySample(side=side, primary_key=key, occurrences=_as_int(row.get("occurrences")))  # type: ignore[arg-type]
            )

    blocking_nulls = {column.lower() for column in [*request.primary_keys, *request.not_null_columns]}
    for row in results.get("null_profile") or []:
        column = str(row.get("column_name"))
        report.null_checks.append(
            NullCheck(
                side=row.get("side") or "source",  # type: ignore[arg-type]
                column=column,
                null_count=_as_int(row.get("null_count")),
                total=_as_int(row.get("total")),
                blocking=column.lower() in blocking_nulls and column.lower() not in {key.lower() for key in request.primary_keys},
            )
        )

    for row in results.get("quality_checks") or []:
        item = FormatCheck(
            side=row.get("side") or "source",  # type: ignore[arg-type]
            column=str(row.get("column_name")),
            rule=str(row.get("rule")),
            failure_count=_as_int(row.get("failure_count")),
            total=_as_int(row.get("total")),
        )
        if item.rule == "json":
            report.deserialization_failures.append(item)
        else:
            report.format_checks.append(item)

    for row in results.get("aggregates") or []:
        dimension = {
            key[len("dim__") :]: _as_str(value)
            for key, value in row.items()
            if key.startswith("dim__")
        }
        if _numbers_differ(row.get("source__count"), row.get("target__count"), 0):
            report.aggregate_mismatches.append(
                AggregateMismatch(
                    dimension=dimension,
                    metric="count",
                    source_value=_as_str(row.get("source__count")),
                    target_value=_as_str(row.get("target__count")),
                )
            )
        for key, value in row.items():
            if not key.startswith("source__") or key == "source__count":
                continue
            metric = key[len("source__") :]
            target_key = f"target__{metric}"
            if _numbers_differ(value, row.get(target_key), request.float_tolerance):
                report.aggregate_mismatches.append(
                    AggregateMismatch(
                        dimension=dimension,
                        metric=metric,
                        source_value=_as_str(value),
                        target_value=_as_str(row.get(target_key)),
                    )
                )

    if duplicate_keys_collapsed and (report.duplicate_keys_source or report.duplicate_keys_target):
        report.warnings.append(
            "Duplicate primary keys were collapsed to one deterministic row so the join could not multiply mismatches."
        )
    used_source = report.source_domain_published_ts
    used_target = report.target_domain_published_ts
    if used_source and used_target and used_source != used_target:
        report.warnings.append(
            "LATE_ARRIVING_OR_UNPUBLISHED: source and target snapshots use different "
            f"{request.timestamp_column} values ({used_source} vs {used_target})."
        )
    if report.source_count == 0 and report.target_count == 0 and not step_errors.get("metrics"):
        report.warnings.append("BOTH_EMPTY: the selected snapshot has zero rows on both sides.")
    if step_errors:
        for name, message in step_errors.items():
            if name != "metrics":
                report.warnings.append(f"{name} could not be computed: {message}")

    report.status_reasons = _status_reasons(request, report)
    if step_errors.get("metrics"):
        report.status = "ERROR"
        report.status_reasons.insert(0, step_errors["metrics"])
    else:
        report.status = "FAIL" if report.status_reasons else "PASS"
    return report


def _status_reasons(request: ValidationRequest, report: ValidationReport) -> list[str]:
    reasons: list[str] = []
    if report.source_count != report.target_count:
        reasons.append(f"Row counts differ (source {report.source_count}, target {report.target_count}).")
    if report.missing_in_target_count:
        reasons.append(f"{report.missing_in_target_count} primary keys are present in source and missing in target.")
    if report.missing_in_source_count:
        reasons.append(f"{report.missing_in_source_count} primary keys are present in target and missing in source.")
    if report.value_mismatch_count:
        reasons.append(f"{report.value_mismatch_count} primary keys differ on mapped columns.")
    if report.duplicate_keys_source or report.duplicate_keys_target:
        reasons.append(
            f"Duplicate primary keys (source {report.duplicate_keys_source}, target {report.duplicate_keys_target})."
        )
    if report.null_pk_source or report.null_pk_target:
        reasons.append(
            f"Null primary keys (source {report.null_pk_source}, target {report.null_pk_target})."
        )
    for mismatch in report.schema_drift.type_mismatches:
        if mismatch.get("severity") == "error":
            reasons.append(
                f"Datatype mismatch on {mismatch['column']}: "
                f"{mismatch['source_type']} vs {mismatch['target_type']}."
            )
            break
    for check in report.null_checks:
        if check.blocking and check.null_count > 0:
            reasons.append(f"Nulls in required column {check.column} on {check.side} ({check.null_count}).")
            break
    for check in [*report.format_checks, *report.deserialization_failures]:
        if check.failure_count > 0:
            reasons.append(f"{check.column} failed {check.rule} on {check.side} ({check.failure_count} rows).")
            break
    if report.aggregate_mismatches:
        reasons.append(f"{len(report.aggregate_mismatches)} aggregate metrics differ.")
    if request.fail_on_timestamp_skew and any(item.startswith("LATE_ARRIVING") for item in report.warnings):
        reasons.append("Source and target published timestamps do not match.")
    return reasons


def _synthesize_columns(request: ValidationRequest) -> list[ColumnSpec]:
    names: list[str] = []
    for name in [
        *request.primary_keys,
        request.timestamp_column,
        *(request.compare_columns or []),
        *request.not_null_columns,
        *request.json_columns,
        *request.formats.keys(),
        *request.aggregate_dimensions,
        *request.aggregate_measures,
    ]:
        if name and name not in names:
            names.append(name)
    if request.soft_delete_column and request.soft_delete_column not in names:
        names.append(request.soft_delete_column)
    return [ColumnSpec(name=name, data_type="string") for name in names]


def resolve_metadata(request: ValidationRequest) -> tuple[list[ColumnSpec], list[ColumnSpec], list[str]]:
    if request.source_columns and request.target_columns:
        return request.source_columns, request.target_columns, []
    if request.execute:
        from app.databricks.client import get_columns

        return get_columns(request.source_table), get_columns(request.target_table), []
    try:
        from app.databricks.client import get_columns

        return (
            get_columns(request.source_table),
            get_columns(request.target_table),
            [],
        )
    except Exception:
        columns = _synthesize_columns(request)
        return (
            columns,
            columns,
            [
                "Live table metadata was not used. Column types default to STRING. "
                "Pass source_columns and target_columns, or configure Databricks, before trusting datatype checks."
            ],
        )


def dqx_quarantine_rows(items: list[Any]) -> list[MismatchRow]:
    """Turn DQX sample keys into quarantine rows the UI can list."""
    rows: list[MismatchRow] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "check")
        message = str(item.get("message") or "")
        criticality = str(item.get("criticality") or "error")
        samples = item.get("samples") or []
        if not samples:
            if not item.get("count"):
                continue
            rows.append(
                MismatchRow(
                    primary_key={"check": name},
                    mismatch_type="VALUE_MISMATCH",
                    column_diffs=[
                        ColumnDiff(column=name, source_value=message or criticality, target_value=f"count={item.get('count')}")
                    ],
                )
            )
            continue
        for sample in samples:
            key: dict[str, str | None] = {}
            for part in str(sample).split(","):
                if "=" not in part:
                    continue
                left, right = part.split("=", 1)
                key[left.strip()] = right.strip()
            if not key:
                key = {"sample": str(sample)}
            rows.append(
                MismatchRow(
                    primary_key=key,
                    mismatch_type="VALUE_MISMATCH",
                    column_diffs=[
                        ColumnDiff(column=name, source_value=message or str(sample), target_value=criticality)
                    ],
                )
            )
    return rows


def merge_dqx_quarantine(report: ValidationReport, items: list[Any]) -> None:
    seen = {
        (json.dumps(row.primary_key, sort_keys=True), tuple(diff.column for diff in row.column_diffs))
        for row in report.quarantine_preview
    }
    for row in dqx_quarantine_rows(items):
        marker = (json.dumps(row.primary_key, sort_keys=True), tuple(diff.column for diff in row.column_diffs))
        if marker in seen:
            continue
        seen.add(marker)
        report.quarantine_preview.append(row)


def _notebook_object(output: str | None) -> dict[str, Any]:
    text = (output or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        return {}
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def run_validation(request: ValidationRequest) -> ValidationOutcome:
    source_location = parse_uc_location(request.source_table)
    parse_uc_location(request.target_table)
    source_columns, target_columns, warnings = resolve_metadata(request)
    steps, drift, plan_warnings, _pairs = build_validation_plan(request, source_columns, target_columns)
    warnings.extend(plan_warnings)
    dqx_checks = build_dqx_checks(
        primary_keys=request.primary_keys,
        not_null_columns=request.not_null_columns,
        formats=request.formats,
        json_columns=request.json_columns,
        compare_columns=[column.name for column, _ in _pairs],
    )
    if not request.execute:
        return ValidationOutcome(
            report=None,
            sql_steps=steps,
            dqx_checks=dqx_checks,
            schema_drift=drift,
            warnings=warnings,
        )

    from app.databricks.client import describe_uc_object, env, run_sql

    for label, name in (("Source", request.source_table), ("Destination", request.target_table)):
        found = describe_uc_object(name)
        if found["checked"] and found["exists"] is False:
            code = "source_missing" if label == "Source" else "destination_missing"
            raise DataflowError(
                f"{label} {name} is not in Unity Catalog. Validation only reads existing tables and volume files.",
                code=code,
            )

    notebook = None
    if env("DATABRICKS_WAREHOUSE_ID"):
        catalog, schema = uc_namespace(source_location)
        results: dict[str, list[dict[str, Any]]] = {}
        step_errors: dict[str, str] = {}
        for step in steps:
            try:
                results[step.name] = run_sql(step.sql, catalog=catalog, schema=schema)
            except DataflowError as exc:
                step_errors[step.name] = exc.message
                if step.critical:
                    break
            except Exception as exc:  # pragma: no cover - network/SDK failures
                step_errors[step.name] = str(exc)
                if step.critical:
                    break
    else:
        from app.databricks.notebook_executor import execute_notebook, render_validation_notebook

        notebook = render_validation_notebook(request, steps)
        status = execute_notebook(
            notebook,
            notebook_name="dataflow_validation",
            wait=True,
            timeout_seconds=600,
        )
        parsed = _notebook_object(status.output)
        results = parsed.get("results") if isinstance(parsed.get("results"), dict) else {}
        step_errors = {key: str(value) for key, value in (parsed.get("errors") or {}).items()}
        if not results:
            raise DataflowError(
                status.error or status.state_message or "Validation notebook did not return a report.",
                code="validation_failed",
                status=502,
            )
    report = assemble_report(
        request,
        results=results,
        step_errors=step_errors,
        drift=drift,
        warnings=warnings,
        dqx_checks=dqx_checks,
        duplicate_keys_collapsed=True,
    )
    return ValidationOutcome(
        report=report,
        sql_steps=steps,
        dqx_checks=dqx_checks,
        notebook=notebook,
        schema_drift=drift,
        warnings=warnings,
    )
