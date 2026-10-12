"""Create parameterized Spark + DQX notebooks and run them as Databricks jobs."""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone

from app.databricks.client import env, get_workspace_client, notebook_dir
from app.databricks.dqx_utils import build_dqx_checks, dqx_checks_yaml
from app.databricks.identifiers import parse_uc_location, require_ident, sql_float, validate_spark_type
from app.errors import DataflowError
from app.schemas import PipelineSpec, RunStatus, SqlStep, ValidationRequest

_NOTEBOOK_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
_BANNED = ("%sh", "%fs", "dbutils.fs.rm", "drop catalog", "drop database", "drop schema")


def _py(value: str) -> str:
    return json.dumps(value)


def _cells(*parts: str) -> str:
    body = "\n\n# COMMAND ----------\n\n".join(part.strip("\n") for part in parts if part.strip())
    return "# Databricks notebook source\n" + body + "\n"


def _guard_code(code: str) -> None:
    if not code.strip():
        raise DataflowError("Notebook code is empty.", code="empty_notebook")
    if len(code) > 1_000_000:
        raise DataflowError("Notebook code exceeds 1 MB.", code="notebook_too_large")
    if "\x00" in code:
        raise DataflowError("Notebook code contains a null byte.", code="invalid_notebook")
    lowered = code.lower()
    for token in _BANNED:
        if token in lowered:
            raise DataflowError(
                f"Notebooks cannot include '{token}'.",
                code="notebook_rejected",
            )


def pipeline_parameters(spec: PipelineSpec) -> dict[str, str]:
    parse_uc_location(spec.source_table)
    parse_uc_location(spec.target_table)
    for key in spec.primary_keys:
        require_ident(key)
    return {
        "source_table": spec.source_table,
        "target_table": spec.target_table,
        "primary_keys": ",".join(spec.primary_keys),
        "timestamp_column": spec.timestamp_column,
        "write_mode": spec.write_mode,
        "float_tolerance": sql_float(spec.float_tolerance),
        "soft_delete_column": spec.soft_delete_column or "",
        "quarantine_suffix": spec.quarantine_suffix or "_quarantine",
        "confirm_create_destination": "true" if spec.confirm_create_destination else "false",
    }


def _repair_block(spec: PipelineSpec) -> str:
    lines = ["df = source_df"]
    for repair in spec.repairs:
        column = require_ident(repair.column)
        col = _py(column)
        action = repair.action
        if action == "trim":
            lines.append(f"df = df.withColumn({col}, F.trim(F.col({col}).cast('string')))")
        elif action == "empty_to_null":
            lines.append(
                f"df = df.withColumn({col}, F.when(F.trim(F.col({col}).cast('string')) == '', F.lit(None)).otherwise(F.col({col})))"
            )
        elif action == "cast":
            spark_type = validate_spark_type(repair.cast_to or "string")
            lines.append(f"df = df.withColumn({col}, F.col({col}).cast({_py(spark_type)}))")
        elif action == "coalesce":
            default = _py(repair.default if repair.default is not None else "")
            lines.append(
                f"df = df.withColumn({col}, F.coalesce(F.col({col}), F.lit({default})))"
            )
        elif action == "parse_timestamp":
            fmt = repair.timestamp_format or "yyyy-MM-dd HH:mm:ss"
            if not re.fullmatch(r"[A-Za-z0-9 :./,_+\-]+", fmt):
                raise DataflowError("Unsupported timestamp format.", code="invalid_format")
            lines.append(f"df = df.withColumn({col}, F.to_timestamp(F.col({col}).cast('string'), {_py(fmt)}))")
        elif action == "parse_json":
            schema = validate_spark_type(repair.json_schema_ddl or "MAP<STRING, STRING>")
            parsed = f"{column}_parsed"
            lines.append(
                f"df = df.withColumn({_py(parsed)}, F.from_json(F.col({col}).cast('string'), {_py(schema)}))"
            )
            if repair.flatten_json and not schema.upper().startswith("MAP"):
                lines.append(f"df = df.select('*', {_py(parsed + '.*')}).drop({_py(parsed)})")
        elif action == "standardize_boolean":
            lines.append(
                "df = df.withColumn(\n"
                f"    {col},\n"
                f"    F.when(F.lower(F.col({col}).cast('string')).isin('y', 'yes', 'true', '1'), F.lit(True))\n"
                f"     .when(F.lower(F.col({col}).cast('string')).isin('n', 'no', 'false', '0'), F.lit(False))\n"
                "     .otherwise(F.lit(None).cast('boolean'))\n"
                ")"
            )
        elif action == "negative_to_null":
            lines.append(
                f"df = df.withColumn({col}, F.when(F.col({col}).cast('double') < 0, F.lit(None).cast('double')).otherwise(F.col({col}).cast('double')))"
            )
        else:
            raise DataflowError(f"Unsupported repair '{action}'.", code="invalid_repair")
    for column in spec.json_columns:
        name = require_ident(column)
        if any(repair.column == name and repair.action == "parse_json" for repair in spec.repairs):
            continue
        parsed = f"{name}_parsed"
        lines.append(
            f"df = df.withColumn({_py(parsed)}, F.from_json(F.col({_py(name)}).cast('string'), 'MAP<STRING, STRING>'))"
        )
    return "\n".join(lines)


def _projection_block(spec: PipelineSpec) -> str:
    if not spec.column_mappings:
        return "projected = df"
    expressions: list[str] = []
    for mapping in spec.column_mappings:
        source = require_ident(mapping.source)
        target = require_ident(mapping.target or mapping.source)
        src = _py(source)
        if mapping.transform == "identity":
            expr = f"F.col({src})"
        elif mapping.transform == "trim":
            expr = f"F.trim(F.col({src}).cast('string'))"
        elif mapping.transform == "lower":
            expr = f"F.lower(F.trim(F.col({src}).cast('string')))"
        elif mapping.transform == "upper":
            expr = f"F.upper(F.trim(F.col({src}).cast('string')))"
        elif mapping.transform == "empty_to_null":
            expr = f"F.when(F.trim(F.col({src}).cast('string')) == '', F.lit(None)).otherwise(F.col({src}))"
        elif mapping.transform == "cast":
            spark_type = validate_spark_type(mapping.cast_to or "string")
            expr = f"F.col({src}).cast({_py(spark_type)})"
        elif mapping.transform == "to_timestamp":
            fmt = mapping.timestamp_format or "yyyy-MM-dd HH:mm:ss"
            if not re.fullmatch(r"[A-Za-z0-9 :./,_+\-]+", fmt):
                raise DataflowError("Unsupported timestamp format.", code="invalid_format")
            expr = f"F.to_timestamp(F.col({src}).cast('string'), {_py(fmt)})"
        elif mapping.transform == "regex_replace":
            pattern = mapping.pattern or ""
            if not pattern or any(token in pattern for token in ["'", "\n"]):
                raise DataflowError("regex_replace needs a single-line pattern.", code="invalid_format")
            expr = (
                f"F.regexp_replace(F.col({src}).cast('string'), {_py(pattern)}, {_py(mapping.replacement or '')})"
            )
        elif mapping.transform == "from_json":
            schema = validate_spark_type(mapping.json_schema_ddl or "MAP<STRING, STRING>")
            expr = f"F.from_json(F.col({src}).cast('string'), {_py(schema)})"
        else:
            raise DataflowError(f"Unsupported mapping transform '{mapping.transform}'.", code="invalid_transform")
        expressions.append(f"    {expr}.alias({_py(target)})")
    joined = ",\n".join(expressions)
    return f"projected = df.select(\n{joined}\n)"


def render_pipeline_notebook(
    spec: PipelineSpec,
    validation_sql: list[SqlStep] | None = None,
    columns: list[str] | None = None,
    learned: list[dict] | None = None,
) -> str:
    params = pipeline_parameters(spec)
    if not re.fullmatch(r"_[A-Za-z0-9_]+", spec.quarantine_suffix or ""):
        raise DataflowError("quarantine_suffix must look like _quarantine.", code="invalid_identifier")
    checks = build_dqx_checks(
        primary_keys=spec.primary_keys,
        not_null_columns=spec.not_null_columns or spec.primary_keys,
        formats=spec.formats,
        json_columns=spec.json_columns,
        compare_columns=spec.compare_columns,
        columns=columns,
    )
    from app.agent.memory import apply_lessons
    from app.agent.skills import validate_check_metadata

    checks = apply_lessons(checks, learned or [])
    validate_check_metadata(checks)
    checks_json = json.dumps(checks, indent=2)
    learned_json = json.dumps(learned or [])
    yaml_text = dqx_checks_yaml(checks)
    generated = datetime.now(timezone.utc).isoformat()
    header = f"""# DataFlow Agent pipeline
# Generated at {generated}
# Source and target are widgets. Change them when the dataset moves; do not edit the logic.
# DQX skills: dqx-define-checks, dqx-apply-checks, dataflow-quality.

import json
import re

from pyspark.sql import functions as F

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _quote_fqn(value: str) -> str:
    parts = [part.strip() for part in value.split(".")]
    if len(parts) != 3 or not all(_IDENT.match(part) for part in parts):
        raise ValueError("Expected catalog.schema.table with simple identifiers, got " + value)
    return ".".join("`" + part + "`" for part in parts)


dbutils.widgets.text("source_table", {_py(params["source_table"])})
dbutils.widgets.text("target_table", {_py(params["target_table"])})
dbutils.widgets.text("primary_keys", {_py(params["primary_keys"])})
dbutils.widgets.text("timestamp_column", {_py(params["timestamp_column"])})
dbutils.widgets.dropdown("write_mode", {_py(params["write_mode"])}, ["merge", "overwrite", "append"])
dbutils.widgets.text("float_tolerance", {_py(params["float_tolerance"])})
dbutils.widgets.text("soft_delete_column", {_py(params["soft_delete_column"])})
dbutils.widgets.text("quarantine_suffix", {_py(params["quarantine_suffix"])})
dbutils.widgets.dropdown(
    "confirm_create_destination",
    {_py(params["confirm_create_destination"])},
    ["false", "true"],
)

source_raw = dbutils.widgets.get("source_table").strip()
target_raw = dbutils.widgets.get("target_table").strip()
primary_keys = [part.strip() for part in dbutils.widgets.get("primary_keys").split(",") if part.strip()]
if not primary_keys or not all(_IDENT.match(part) for part in primary_keys):
    raise ValueError("primary_keys must be comma-separated identifiers")
timestamp_column = dbutils.widgets.get("timestamp_column").strip()
if not _IDENT.match(timestamp_column):
    raise ValueError("timestamp_column is not a safe identifier")
write_mode = dbutils.widgets.get("write_mode").strip()
soft_delete_column = dbutils.widgets.get("soft_delete_column").strip()
quarantine_suffix = dbutils.widgets.get("quarantine_suffix").strip() or "_quarantine"
confirm_create_destination = dbutils.widgets.get("confirm_create_destination").strip().lower() == "true"
"""
    helpers = '''
_VOLUME_SEGMENT = re.compile(r"^[A-Za-z0-9_\\-]+(?:\\.[A-Za-z0-9_\\-]+)*$")


def _is_volume(value):
    return value.startswith("/Volumes/")


def _validate_volume(path):
    parts = [part for part in path.split("/") if part]
    if len(parts) < 5 or parts[0] != "Volumes":
        raise ValueError("Expected /Volumes/catalog/schema/volume/file.csv")
    for part in parts[1:]:
        if not _VOLUME_SEGMENT.match(part):
            raise ValueError("Unsafe Unity Catalog volume path: " + path)
    lower = parts[-1].lower()
    if not lower.endswith((".csv", ".txt", ".tsv", ".xlsx", ".xls", ".xlsm")):
        raise ValueError("Volume files must be CSV or Excel")


def _volume_exists(path):
    _validate_volume(path)
    parent, name = path.rsplit("/", 1)
    try:
        entries = dbutils.fs.ls(parent)
    except Exception:
        return False
    return any(getattr(entry, "name", "").rstrip("/") == name for entry in entries)


def _object_exists(raw):
    if _is_volume(raw):
        return _volume_exists(raw)
    _quote_fqn(raw)
    return spark.catalog.tableExists(raw)


def _read_uc(raw):
    if not _object_exists(raw):
        raise ValueError("Source is not in Unity Catalog: " + raw)
    if _is_volume(raw):
        lower = raw.lower()
        if lower.endswith((".xlsx", ".xls", ".xlsm")):
            import pandas as pd
            return spark.createDataFrame(pd.read_excel(raw))
        return spark.read.option("header", True).option("inferSchema", True).csv(raw)
    return spark.table(_quote_fqn(raw))


def _checks_from_frame(frame, keys):
    """DQX rules from the source that was just read. A different source builds different rules."""
    rows = frame.limit(40).collect()
    checks = []
    seen = set()

    def add(name, function, arguments, criticality="error"):
        if name in seen:
            return
        seen.add(name)
        checks.append({"name": name, "criticality": criticality, "check": {"function": function, "arguments": arguments}})

    money = ("amount", "price", "cost", "total", "revenue", "balance", "fee")
    quantity = ("qty", "quantity", "count", "units")
    json_names = ("payload", "extra_info", "metadata", "attributes")
    category_names = ("status", "state", "category")
    numeric_types = ("int", "bigint", "integer", "long", "short", "double", "float", "decimal")
    date_types = ("date", "timestamp")
    for field in frame.schema.fields:
        original = field.name
        if not _IDENT.match(original):
            continue
        lower = original.lower()
        data_type = field.dataType.simpleString().lower()
        values = []
        for row in rows:
            value = row[original]
            if value is not None and str(value).strip():
                values.append(str(value))
        ident = "`" + original + "`"
        if lower == "id" or lower.endswith("_id") or lower in keys:
            add(original + "_not_null", "is_not_null", {"column": original})
        if "email" in lower:
            add(original + "_not_null", "is_not_null", {"column": original}, "warn")
            add(original + "_valid_format", "sql_expression", {
                "expression": ident + " IS NULL OR CAST(" + ident + " AS STRING) RLIKE '^[A-Za-z0-9._%+\\\\-]+@[A-Za-z0-9.\\\\-]+\\\\.[A-Za-z]{2,}$'",
                "msg": original + " must look like user@domain.tld",
                "negate": False,
            })
        if ("date" in lower or lower.endswith("_at") or lower.endswith("_ts") or lower.endswith("_dt")) and not data_type.startswith(date_types):
            add(original + "_is_valid_date", "sql_expression", {
                "expression": ident + " IS NULL OR try_to_date(CAST(" + ident + " AS STRING)) IS NOT NULL OR try_to_date(CAST(" + ident + " AS STRING), 'dd/MM/yyyy') IS NOT NULL OR try_to_date(CAST(" + ident + " AS STRING), 'MM/dd/yyyy') IS NOT NULL",
                "msg": original + " must be a parseable date",
                "negate": False,
            })
        is_money = lower in money or any(lower.endswith("_" + token) for token in money)
        is_quantity = lower in quantity or any(lower.endswith("_" + token) for token in quantity)
        if is_money and data_type.startswith(numeric_types):
            add(original + "_greater_than_zero", "sql_expression", {
                "expression": ident + " IS NULL OR " + ident + " > 0",
                "msg": original + " must be greater than zero",
                "negate": False,
            })
        elif is_money or is_quantity:
            numeric = "^-?[0-9]+(\\\\.[0-9]+)?$" if is_quantity and not is_money else "^[0-9]+(\\\\.[0-9]+)?$"
            add(original + "_is_numeric", "sql_expression", {
                "expression": ident + " IS NULL OR CAST(" + ident + " AS STRING) RLIKE '" + numeric + "'",
                "msg": original + " must be numeric",
                "negate": False,
            })
            if is_money:
                add(original + "_greater_than_zero", "sql_expression", {
                    "expression": ident + " IS NULL OR NOT (CAST(" + ident + " AS STRING) RLIKE '" + numeric + "') OR CAST(" + ident + " AS DECIMAL(18,2)) > 0",
                    "msg": original + " must be greater than zero",
                    "negate": False,
                })
        if "json" in lower or lower in json_names or any(value.lstrip()[:1] in "{[" for value in values):
            add(original + "_valid_json", "sql_expression", {
                "expression": ident + " IS NULL OR TRIM(CAST(" + ident + " AS STRING)) = '' OR lower(TRIM(CAST(" + ident + " AS STRING))) = 'null' OR try_parse_json(CAST(" + ident + " AS STRING)) IS NOT NULL",
                "msg": original + " should be valid JSON, blank, or null",
                "negate": False,
            }, "warn")
        if lower in category_names:
            add(original + "_not_null", "is_not_null", {"column": original})
            groups = {}
            for value in values:
                token = value.strip()
                if token and token.lower() != "null":
                    groups.setdefault(token.lower(), []).append(token)
            mixed = {}
            for token, group in groups.items():
                if len(set(group)) > 1:
                    mixed[token] = max(set(group), key=group.count)
            if mixed and len(groups) <= 12:
                allowed = ", ".join(json.dumps(token) for token in sorted(mixed))
                canonical = ", ".join(json.dumps(mixed[token]) for token in sorted(mixed))
                add(original + "_case_inconsistent", "sql_expression", {
                    "expression": ident + " IS NULL OR lower(CAST(" + ident + " AS STRING)) NOT IN (" + allowed + ") OR CAST(" + ident + " AS STRING) IN (" + canonical + ")",
                    "msg": original + " casing is inconsistent with the other values in this source",
                    "negate": False,
                }, "warn")
    if len(keys) > 1 or (len(keys) == 1 and keys[0]):
        safe_keys = [key for key in keys if _IDENT.match(key)]
        if safe_keys:
            add("primary_key_is_unique", "is_unique", {"columns": safe_keys})
    return checks


def _apply_learned(checks, learned):
    """User lessons apply at once. Lessons from repeated runs apply from the second time."""
    active = []
    for item in learned or []:
        weight = int(item.get("weight") or 1)
        origin = item.get("origin") or "user"
        if origin == "user" and weight >= 1:
            active.append(item)
        elif origin != "user" and weight >= 2:
            active.append(item)
    skip = set()
    for item in active:
        if item.get("kind") == "skip_column" and item.get("column"):
            skip.add(str(item["column"]).lower())
    kept = []
    for check in checks:
        arguments = ((check.get("check") or {}).get("arguments") or {})
        column = arguments.get("column")
        name = str(check.get("name") or "").lower()
        if isinstance(column, str) and column.lower() in skip:
            continue
        if any(name.startswith(item + "_") for item in skip):
            continue
        kept.append(check)
    names = set(item["name"] for item in kept)
    for item in active:
        column = str(item.get("column") or "")
        if not _IDENT.match(column):
            continue
        kind = item.get("kind")
        if kind == "not_null" and column + "_not_null" not in names:
            kept.append({"name": column + "_not_null", "criticality": "error", "check": {"function": "is_not_null", "arguments": {"column": column}}})
            names.add(column + "_not_null")
        elif kind == "json" and column + "_valid_json" not in names:
            ident = "`" + column + "`"
            expression = ident + " IS NULL OR TRIM(CAST(" + ident + " AS STRING)) = '' OR lower(TRIM(CAST(" + ident + " AS STRING))) = 'null' OR try_parse_json(CAST(" + ident + " AS STRING)) IS NOT NULL"
            kept.append({"name": column + "_valid_json", "criticality": "warn", "check": {"function": "sql_expression", "arguments": {"expression": expression, "msg": column + " should be valid JSON", "negate": False}}})
            names.add(column + "_valid_json")
        elif kind == "allowed_values" and column + "_allowed_values" not in names:
            values = [part.strip() for part in str(item.get("detail") or "").split(",") if part.strip()][:20]
            if not values:
                continue
            ident = "`" + column + "`"
            quoted = ", ".join(json.dumps(value) for value in values)
            expression = ident + " IS NULL OR CAST(" + ident + " AS STRING) IN (" + quoted + ")"
            kept.append({"name": column + "_allowed_values", "criticality": "error", "check": {"function": "sql_expression", "arguments": {"expression": expression, "msg": column + " must be one of the learned values", "negate": False}}})
            names.add(column + "_allowed_values")
    return kept
'''
    read = f"""if not _object_exists(target_raw) and not confirm_create_destination:
    payload = json.dumps({{
        "status": "confirmation_required",
        "destination": target_raw,
        "message": "Destination is not in Unity Catalog. Confirm creation before writing it.",
    }})
    print(payload)
    dbutils.notebook.exit(payload)

source_df = _read_uc(source_raw)
if timestamp_column in source_df.columns:
    latest = source_df.agg(F.max(F.col(timestamp_column)).alias("_max_ts")).collect()[0]["_max_ts"]
    source_df = source_df.filter(F.col(timestamp_column) == F.lit(latest))
    print("Using source " + timestamp_column + "=" + str(latest))
else:
    print("Source has no " + timestamp_column + "; reading the full table.")

if soft_delete_column and soft_delete_column in source_df.columns:
    deleted = F.lower(F.col(soft_delete_column).cast("string")).isin("true", "1", "yes", "y")
    source_df = source_df.filter(~F.coalesce(deleted, F.lit(False)))
"""
    quality = f"""DQX_SPEC = {checks_json}
try:
    DQX_CHECKS = _checks_from_frame(projected, primary_keys)
except Exception as _dqx_build_error:
    print("Using column rules from the notebook spec. Live rule build failed: " + str(_dqx_build_error))
    DQX_CHECKS = list(DQX_SPEC)
_check_names = set(item["name"] for item in DQX_CHECKS)
LEARNED_RULES = {learned_json}
DQX_CHECKS = _apply_learned(DQX_CHECKS, LEARNED_RULES)
_check_names = set(item["name"] for item in DQX_CHECKS)
for _spec_check in DQX_SPEC:
    if _spec_check["name"] not in _check_names:
        DQX_CHECKS.append(_spec_check)
        _check_names.add(_spec_check["name"])

# Equivalent YAML for operators reviewing the run:
# {yaml_text.replace(chr(10), chr(10) + "# ")}

def _load_engine():
    try:
        from databricks.labs.dqx.engine import DQEngine
    except ImportError:
        import subprocess
        import sys
        subprocess.check_call([sys.executable, "-m", "pip", "install", "databricks-labs-dqx"])
        from databricks.labs.dqx.engine import DQEngine
    from databricks.sdk import WorkspaceClient
    return DQEngine(WorkspaceClient())


dqx_summary = []
if DQX_CHECKS:
    engine = _load_engine()
    apply_checks = getattr(engine, "apply_checks_by_metadata", None)
    if apply_checks is None:
        raise RuntimeError("Installed databricks-labs-dqx cannot apply metadata checks.")
    checked = apply_checks(projected, DQX_CHECKS)

    def _rule_hits(frame, column_name, criticality):
        if column_name not in frame.columns:
            return []
        key_cols = [F.col(key).cast("string").alias(key) for key in primary_keys if key in frame.columns]
        exploded = frame.filter(F.col(column_name).isNotNull() & (F.size(F.col(column_name)) > 0)).select(
            F.explode(F.col(column_name)).alias("_hit"),
            *key_cols,
        )
        grouped = exploded.groupBy(F.col("_hit.name").alias("name"), F.col("_hit.message").alias("message")).agg(
            F.count("*").alias("count"),
            F.slice(F.collect_list(F.struct(*[F.col(key) for key in primary_keys if key in frame.columns])), 1, 5).alias("samples"),
        )
        hits = []
        for row in grouped.collect():
            sample_text = []
            for sample in row["samples"] or []:
                values = sample.asDict() if hasattr(sample, "asDict") else {{}}
                sample_text.append(", ".join(key + "=" + str(values.get(key)) for key in primary_keys if key in values))
            hits.append({{
                "name": row["name"],
                "message": row["message"],
                "count": int(row["count"]),
                "criticality": criticality,
                "samples": sample_text,
            }})
        return hits

    dqx_summary = _rule_hits(checked, "_errors", "error") + _rule_hits(checked, "_warnings", "warn")
    for item in dqx_summary:
        print(item["criticality"] + " " + str(item["name"]) + " count=" + str(item["count"]) + " " + "; ".join(item["samples"]))
    valid_df = checked.filter(F.col("_errors").isNull() | (F.size(F.col("_errors")) == 0)).drop("_errors", "_warnings")
    quarantine_df = checked.filter(F.col("_errors").isNotNull() & (F.size(F.col("_errors")) > 0))
else:
    valid_df = projected
    quarantine_df = projected.limit(0)
"""
    write = '''
destination_exists = _object_exists(target_raw)
if not destination_exists and not confirm_create_destination:
    payload = json.dumps({
        "status": "confirmation_required",
        "destination": target_raw,
        "message": "Destination is not in Unity Catalog. Confirm creation before writing it.",
    })
    print(payload)
    dbutils.notebook.exit(payload)

def _write_table(frame, table_name, mode):
    if mode == "overwrite" or not spark.catalog.tableExists(table_name):
        frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(table_name)
    elif mode == "append":
        frame.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(table_name)
    elif mode == "merge":
        key_match = " AND ".join(["t.`" + key + "` = s.`" + key + "`" for key in primary_keys])
        frame.createOrReplaceTempView("_dataflow_staged")
        spark.sql(
            "MERGE INTO " + _quote_fqn(table_name) + " t "
            "USING _dataflow_staged s ON " + key_match + " "
            "WHEN MATCHED THEN UPDATE SET * "
            "WHEN NOT MATCHED THEN INSERT *"
        )
    else:
        raise ValueError("write_mode must be merge, overwrite, or append")


def _write_file(frame, path, mode):
    lower = path.lower()
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        import pandas as pd
        pdf = frame.toPandas()
        if mode == "append":
            try:
                existing = pd.read_excel(path)
                pdf = pd.concat([existing, pdf], ignore_index=True)
            except Exception:
                pass
        pdf.to_excel(path, index=False)
        return
    import pandas as pd
    pdf = frame.toPandas()
    if mode == "append":
        try:
            existing = pd.read_csv(path)
            pdf = pd.concat([existing, pdf], ignore_index=True)
        except Exception:
            pass
    pdf.to_csv(path, index=False)


if _is_volume(target_raw):
    _validate_volume(target_raw)
    parent, filename = target_raw.rsplit("/", 1)
    stem, ext = filename.rsplit(".", 1)
    quarantine_raw = parent + "/" + stem + quarantine_suffix + "." + ext
else:
    catalog, schema, table = target_raw.split(".")
    quarantine_raw = catalog + "." + schema + "." + table + quarantine_suffix

valid_count = valid_df.count()
quarantine_count = quarantine_df.count()
print("valid_rows=" + str(valid_count) + " quarantine_rows=" + str(quarantine_count))

if quarantine_count:
    if _is_volume(quarantine_raw):
        _write_file(quarantine_df, quarantine_raw, "append")
    else:
        quarantine_df.write.format("delta").mode("append").option("mergeSchema", "true").saveAsTable(quarantine_raw)

if _is_volume(target_raw):
    _write_file(valid_df, target_raw, write_mode)
else:
    _write_table(valid_df, target_raw, write_mode)
'''
    parts = [header + helpers, read, _repair_block(spec), _projection_block(spec), quality, write]
    if validation_sql:
        parts.append(_validation_runner(validation_sql))
    else:
        parts.append(
            """summary = {
    "valid_rows": valid_count,
    "quarantine_rows": quarantine_count,
    "target": target_raw,
    "quarantine": quarantine_raw,
    "dqx": dqx_summary,
}
payload = json.dumps(summary)
print(payload)
dbutils.notebook.exit(payload)
"""
        )
    code = _cells(*parts)
    _guard_code(code)
    return code


def _validation_runner(steps: list[SqlStep]) -> str:
    serialized = json.dumps([{"name": step.name, "sql": step.sql, "critical": step.critical} for step in steps])
    return f"""VALIDATION_STEPS = json.loads({_py(serialized)})
results = {{}}
errors = {{}}
for step in VALIDATION_STEPS:
    try:
        frame = spark.sql(step["sql"])
        results[step["name"]] = [row.asDict(recursive=True) for row in frame.collect()]
    except Exception as exc:
        errors[step["name"]] = str(exc)
        if step.get("critical"):
            break
copy = {{}}
try:
    copy["valid_rows"] = int(valid_count)
    copy["quarantine_rows"] = int(quarantine_count)
    copy["target"] = target_raw
except Exception:
    pass
payload = json.dumps({{"copy": copy, "results": results, "errors": errors, "dqx": dqx_summary}}, default=str)
print(payload)
dbutils.notebook.exit(payload)
"""


def render_validation_notebook(request: ValidationRequest, steps: list[SqlStep]) -> str:
    generated = datetime.now(timezone.utc).isoformat()
    header = f"""# DataFlow Agent source-to-target validation
# Generated at {generated}
# Source: {request.source_table}
# Target: {request.target_table}
# Grain: {", ".join(request.primary_keys)}
# Snapshot column: {request.timestamp_column} ({request.timestamp_alignment})

import json
"""
    code = _cells(header, _validation_runner(steps))
    _guard_code(code)
    return code


def _notebook_path(name: str | None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    raw = name or f"dataflow_{stamp}"
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "_", raw).strip("_")[:80] or f"dataflow_{stamp}"
    if not _NOTEBOOK_NAME.match(cleaned):
        raise DataflowError("Notebook name is not safe.", code="invalid_path")
    return f"{notebook_dir()}/generated/{cleaned}"


def upload_notebook(code: str, notebook_name: str | None = None) -> str:
    _guard_code(code)
    path = _notebook_path(notebook_name)
    client = get_workspace_client()
    parent = path.rsplit("/", 1)[0]
    client.workspace.mkdirs(parent)
    payload = code.encode("utf-8")
    from databricks.sdk.service.workspace import ImportFormat, Language

    upload = getattr(client.workspace, "upload", None)
    if upload is not None:
        try:
            upload(path, payload, format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
            return path
        except TypeError:
            import io

            upload(path, io.BytesIO(payload), format=ImportFormat.SOURCE, language=Language.PYTHON, overwrite=True)
            return path
    import base64

    client.workspace.import_(
        path=path,
        content=base64.b64encode(payload).decode("ascii"),
        format=ImportFormat.SOURCE,
        language=Language.PYTHON,
        overwrite=True,
    )
    return path


def _enum_value(value) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _submit(path: str, base_parameters: dict[str, str]) -> int:
    client = get_workspace_client()
    from databricks.sdk.service import jobs

    notebook_task = jobs.NotebookTask(
        notebook_path=path,
        base_parameters={key: str(value) for key, value in base_parameters.items()} or None,
    )
    cluster_id = env("DATABRICKS_CLUSTER_ID")
    serverless = (env("DATABRICKS_USE_SERVERLESS", "true") or "true").lower() != "false"
    environments = None
    task_kwargs = {"task_key": "dataflow", "notebook_task": notebook_task}
    if cluster_id:
        task_kwargs["existing_cluster_id"] = cluster_id
    elif serverless:
        environment = _serverless_environment(jobs)
        task_kwargs["environment_key"] = "dataflow"
        environments = [environment]
    else:
        raise DataflowError(
            "Set DATABRICKS_CLUSTER_ID or DATABRICKS_USE_SERVERLESS=true to run notebooks.",
            code="compute_not_configured",
            status=503,
        )
    submit_kwargs = {
        "run_name": "dataflow-agent",
        "tasks": [jobs.SubmitTask(**task_kwargs)],
    }
    if environments is not None:
        submit_kwargs["environments"] = environments
    try:
        waiter = client.jobs.submit(**submit_kwargs)
    except TypeError:
        if cluster_id:
            raise
        task_kwargs.pop("environment_key", None)
        task_kwargs["existing_cluster_id"] = cluster_id
        raise DataflowError(
            "This databricks-sdk build could not submit a serverless notebook task. Set DATABRICKS_CLUSTER_ID.",
            code="compute_not_configured",
            status=503,
        )
    run_id = getattr(waiter, "run_id", None)
    if run_id is None and hasattr(waiter, "response"):
        run_id = waiter.response.run_id
    if run_id is None:
        raise DataflowError("Databricks did not return a run id.", code="submit_failed", status=502)
    return int(run_id)


def _serverless_environment(jobs):
    from databricks.sdk.service.compute import Environment

    spec = Environment(environment_version="2", dependencies=["databricks-labs-dqx"])
    return jobs.JobEnvironment(environment_key="dataflow", spec=spec)


def get_run_status(run_id: int) -> RunStatus:
    client = get_workspace_client()
    run = client.jobs.get_run(run_id=run_id)
    state = run.state
    life = _enum_value(state.life_cycle_state) if state else None
    result = _enum_value(state.result_state) if state else None
    message = state.state_message if state else None
    finished = life in {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
    output_text = None
    error = None
    if finished:
        task_run_id = run_id
        if run.tasks:
            task_run_id = run.tasks[0].run_id or run_id
        try:
            output = client.jobs.get_run_output(run_id=task_run_id)
            if getattr(output, "notebook_output", None) is not None:
                output_text = output.notebook_output.result
            if getattr(output, "error", None):
                error = output.error
            trace = getattr(output, "error_trace", None)
            if trace and not output_text:
                output_text = trace
        except Exception as exc:  # pragma: no cover - depends on run shape
            error = str(exc)
    return RunStatus(
        run_id=run_id,
        life_cycle_state=life,
        result_state=result,
        state_message=message,
        run_page_url=getattr(run, "run_page_url", None),
        finished=finished,
        output=output_text,
        error=error,
    )


def execute_notebook(
    code: str,
    *,
    notebook_name: str | None = None,
    base_parameters: dict[str, str] | None = None,
    wait: bool = True,
    timeout_seconds: int = 180,
) -> RunStatus:
    path = upload_notebook(code, notebook_name)
    run_id = _submit(path, base_parameters or {})
    status = get_run_status(run_id)
    status.notebook_path = path
    if not wait:
        return status
    timeout = max(5, min(timeout_seconds, 3600))
    deadline = time.time() + timeout
    while not status.finished and time.time() < deadline:
        time.sleep(3)
        status = get_run_status(run_id)
        status.notebook_path = path
    if not status.finished:
        status.state_message = (
            (status.state_message or "Notebook is still running.") + " Poll GET /api/runs/{run_id}."
        )
    return status
