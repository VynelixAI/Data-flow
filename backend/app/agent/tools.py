"""Tools the agent can call. Every Databricks action goes through these functions."""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from app.databricks.client import describe_uc_object, env, get_columns, list_tables, run_sql
from app.databricks.dqx_utils import build_dqx_checks, suggest_repairs
from app.databricks.identifiers import locations_in_text, parse_uc_location, quote_ident, require_ident, sql_relation
from app.databricks.notebook_executor import (
    execute_notebook,
    get_run_status,
    pipeline_parameters,
    render_pipeline_notebook,
    render_validation_notebook,
)
from app.databricks.validation import assemble_report, build_validation_plan, run_validation, type_family
from app.errors import DataflowError
from app.schemas import ColumnSpec, PipelineSpec, RunStatus, ValidationRequest

LABELS = {
    "list_tables": "Listing Unity Catalog tables",
    "describe_table": "Reading table schema",
    "profile_table": "Profiling columns",
    "search_catalog": "Searching Unity Catalog",
    "inspect_uc_object": "Checking Unity Catalog",
    "create_destination": "Creating the Unity Catalog destination",
    "generate_pipeline": "Copying data and running validation",
    "validate_source_to_target": "Validating source against target",
    "execute_notebook": "Running notebook on Databricks",
    "get_job_run_status": "Checking Databricks run",
    "remember_lesson": "Saving a lesson",
    "recall_memory": "Recalling earlier lessons",
    "load_skill": "Loading an agent skill",
}


def _function(name: str, description: str, properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
                "additionalProperties": False,
            },
        },
    }


_STRING = {"type": "string"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}
_BOOL = {"type": "boolean"}
_NUMBER = {"type": "number"}

TOOL_SPECS = [
    _function(
        "list_tables",
        "List tables in a Unity Catalog schema.",
        {"catalog": _STRING, "schema_name": _STRING},
        ["catalog", "schema_name"],
    ),
    _function(
        "search_catalog",
        "Search Unity Catalog for tables and CSV/Excel files by a partial name. "
        "Use this when the user describes a dataset instead of giving catalog.schema.table or a /Volumes path. "
        "Does not create anything.",
        {"query": _STRING, "catalog": _STRING, "schema_name": _STRING},
        ["query"],
    ),
    _function(
        "inspect_uc_object",
        "Check whether a Unity Catalog table (catalog.schema.table) or volume file "
        "(/Volumes/catalog/schema/volume/file.csv or .xlsx) exists. Does not create anything.",
        {"name": _STRING},
        ["name"],
    ),
    _function(
        "create_destination",
        "Create a missing Unity Catalog table or volume CSV/Excel file. "
        "Set confirm true only after the user explicitly says yes. "
        "An empty object is created with the source schema. Rows are loaded later by the pipeline.",
        {"source_table": _STRING, "target_table": _STRING, "confirm": _BOOL},
        ["source_table", "target_table", "confirm"],
    ),
    _function(
        "describe_table",
        "Return column names and types for a Unity Catalog table or a CSV/Excel file in a volume.",
        {"table": _STRING},
        ["table"],
    ),
    _function(
        "profile_table",
        "Profile the latest snapshot: nulls, distinct counts, samples, and repair suggestions. Requires a SQL warehouse.",
        {
            "table": _STRING,
            "timestamp_column": _STRING,
        },
        ["table"],
    ),
    _function(
        "generate_pipeline",
        "Copy the source into the destination on Databricks and run the validation SQL in that same notebook. "
        "Use this once the source exists and the destination exists or the user has agreed to create it. "
        "If the destination does not exist yet, leave confirm_create_destination false; the tool then only asks "
        "for confirmation and does not copy rows.",
        {
            "source_table": _STRING,
            "target_table": _STRING,
            "primary_keys": _STRING_LIST,
            "compare_columns": _STRING_LIST,
            "repairs": {"type": "array", "items": {"type": "object"}},
            "column_mappings": {"type": "array", "items": {"type": "object"}},
            "json_columns": _STRING_LIST,
            "timestamp_column": _STRING,
            "soft_delete_column": _STRING,
            "write_mode": {"type": "string", "enum": ["merge", "overwrite", "append"]},
            "not_null_columns": _STRING_LIST,
            "formats": {"type": "object"},
            "float_tolerance": _NUMBER,
            "confirm_create_destination": _BOOL,
            "notes": _STRING,
        },
        ["source_table", "target_table", "primary_keys"],
    ),
    _function(
        "validate_source_to_target",
        "Build and optionally execute source-to-target validation for the latest published snapshot. "
        "Set execute true to run it on the SQL warehouse. Set execute false to only generate SQL, DQX checks, and a notebook.",
        {
            "source_table": _STRING,
            "target_table": _STRING,
            "primary_keys": _STRING_LIST,
            "compare_columns": _STRING_LIST,
            "timestamp_column": _STRING,
            "timestamp_alignment": {"type": "string", "enum": ["latest_each", "common_max"]},
            "source_published_ts": _STRING,
            "target_published_ts": _STRING,
            "float_tolerance": _NUMBER,
            "null_equivalence": _BOOL,
            "soft_delete_column": _STRING,
            "not_null_columns": _STRING_LIST,
            "json_columns": _STRING_LIST,
            "formats": {"type": "object"},
            "aggregate_dimensions": _STRING_LIST,
            "aggregate_measures": _STRING_LIST,
            "execute": _BOOL,
            "fail_on_timestamp_skew": _BOOL,
        },
        ["source_table", "target_table", "primary_keys"],
    ),
    _function(
        "execute_notebook",
        "Create a workspace notebook and start a Databricks job. confirm must be true, and only after the user asked to run it.",
        {
            "code": _STRING,
            "notebook_name": _STRING,
            "base_parameters": {"type": "object"},
            "confirm": _BOOL,
            "wait": _BOOL,
        },
        ["code", "confirm"],
    ),
    _function(
        "get_job_run_status",
        "Fetch the life cycle, result, and notebook output for a Databricks run id.",
        {"run_id": {"type": "integer"}},
        ["run_id"],
    ),
    _function(
        "recall_memory",
        "Recall lessons learned from earlier sources. Pass the current source column names.",
        {"columns": _STRING_LIST},
        ["columns"],
    ),
    _function(
        "remember_lesson",
        "Remember a correction so later sources improve. Use skip_column, not_null, format, json, "
        "allowed_values, primary_keys, or write_mode. Pass the source column names in columns. "
        "The same lesson again increases its weight. Do not store row values.",
        {
            "kind": {
                "type": "string",
                "enum": ["skip_column", "not_null", "format", "json", "allowed_values", "primary_keys", "write_mode"],
            },
            "column": _STRING,
            "detail": _STRING,
            "columns": _STRING_LIST,
        },
        ["kind"],
    ),
    _function(
        "load_skill",
        "Load one Agent Skill by name. Call this when a listed skill matches the task, "
        "before you define or apply DQX checks. Pass dataflow-quality before generate_pipeline.",
        {"name": _STRING},
        ["name"],
    ),
]


def _listify(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [part.strip() for part in value.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return None


def _model_error(exc: ValidationError) -> dict[str, Any]:
    return {"error": exc.errors()[0]["msg"] if exc.errors() else "Invalid arguments", "code": "invalid_arguments"}


def _truncate(text: str, limit: int = 3500) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n# ... full notebook is in the code panel"


def profile_table(table: str, timestamp_column: str = "domain_published_ts") -> dict[str, Any]:
    columns = get_columns(table)
    require_ident(timestamp_column)
    chosen = columns[:40]
    metrics = ["COUNT(*) AS row_count"]
    for column in chosen:
        ident = quote_ident(column.name)
        metrics.append(f"SUM(CASE WHEN {ident} IS NULL THEN 1 ELSE 0 END) AS nulls__{column.name}")
        metrics.append(f"SUM(CASE WHEN CAST({ident} AS STRING) = '' THEN 1 ELSE 0 END) AS empty__{column.name}")
        metrics.append(f"APPROX_COUNT_DISTINCT({ident}) AS distinct__{column.name}")
    table_sql = sql_relation(table)
    ts = quote_ident(timestamp_column)
    has_timestamp = any(column.name.lower() == timestamp_column.lower() for column in columns)
    if has_timestamp:
        relation = f"""(
  SELECT * FROM {table_sql}
  WHERE {ts} = (SELECT MAX({ts}) FROM {table_sql})
)"""
    else:
        relation = table_sql
    summary_rows = run_sql(f"SELECT {', '.join(metrics)} FROM {relation} snapshot")
    summary = summary_rows[0] if summary_rows else {}
    sample_rows = run_sql(f"SELECT * FROM {relation} snapshot LIMIT 5")
    profile_columns = []
    for column in chosen:
        samples = []
        for row in sample_rows:
            value = row.get(column.name)
            if value is not None:
                samples.append(str(value)[:180])
        profile_columns.append(
            {
                "name": column.name,
                "data_type": column.data_type,
                "family": type_family(column.data_type),
                "null_count": int(float(summary.get(f"nulls__{column.name}") or 0)),
                "empty_string_count": int(float(summary.get(f"empty__{column.name}") or 0)),
                "approx_distinct": summary.get(f"distinct__{column.name}"),
                "samples": samples[:3],
            }
        )
    profile = {
        "table": table,
        "row_count": int(float(summary.get("row_count") or 0)),
        "timestamp_column": timestamp_column if has_timestamp else None,
        "columns": profile_columns,
        "truncated": len(columns) > len(chosen),
    }
    profile["repair_suggestions"] = suggest_repairs(profile)
    return profile


def _confirmation(destination: str, message: str, *, kind: str, source: str) -> dict[str, Any]:
    return {
        "result": {
            "confirmation_required": True,
            "destination": destination,
            "source": source,
            "kind": kind,
            "message": message,
        },
        "artifacts": [
            {
                "kind": "confirmation",
                "title": "Create destination?",
                "destination": destination,
                "source": source,
                "message": message,
            }
        ],
    }


def _validation_request(spec: PipelineSpec) -> ValidationRequest:
    return ValidationRequest(
        source_table=spec.source_table,
        target_table=spec.target_table,
        primary_keys=spec.primary_keys,
        compare_columns=spec.compare_columns or None,
        timestamp_column=spec.timestamp_column,
        soft_delete_column=spec.soft_delete_column,
        not_null_columns=spec.not_null_columns,
        json_columns=spec.json_columns,
        formats=spec.formats,
        float_tolerance=spec.float_tolerance,
        execute=False,
        include_notebook=False,
    )


def _column_specs(name: str) -> list[ColumnSpec]:
    location = parse_uc_location(name)
    if location.kind == "table" or env("DATABRICKS_WAREHOUSE_ID"):
        return get_columns(name)
    from app.databricks.destination import _columns_from_volume_file

    names = _columns_from_volume_file(location)
    return [ColumnSpec(name=column, data_type="string") for column in names]


def _columns_for_copy(source: str, target: str) -> tuple[list[ColumnSpec], list[ColumnSpec], list[str]]:
    """Column metadata for the copy. A volume file can be described without a SQL warehouse."""
    warnings: list[str] = []
    source_columns = _column_specs(source)
    try:
        target_columns = _column_specs(target)
    except DataflowError as exc:
        if parse_uc_location(target).kind != "volume_file":
            raise
        target_columns = list(source_columns)
        warnings.append(
            f"Destination file columns were taken from the source ({exc.message}). "
            "The notebook still writes the file and validates the copied rows."
        )
    if target_columns and source_columns and all(column.data_type == "string" for column in target_columns):
        typed = {column.name.lower(): column for column in source_columns}
        aligned: list[ColumnSpec] = []
        for column in target_columns:
            known = typed.get(column.name.lower())
            aligned.append(known if known is not None else column)
        if any(column.data_type != "string" for column in aligned):
            target_columns = aligned
            warnings.append("Volume file types were aligned to the source so datatype checks follow the table.")
    return source_columns, target_columns, warnings


def _parse_notebook_payload(output: str | None) -> dict[str, Any]:
    text = (output or "").strip()
    if not text:
        return {}
    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {}


def _run_copy(spec: PipelineSpec, notebook: str) -> RunStatus:
    parameters = dict(pipeline_parameters(spec))
    # This runs only after the destination exists, or the operator agreed to create it.
    parameters["confirm_create_destination"] = "true"
    return execute_notebook(
        notebook,
        notebook_name="copy_and_validate",
        base_parameters=parameters,
        wait=True,
        timeout_seconds=600,
    )


def _generate(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments)
    for key in ("primary_keys", "compare_columns", "json_columns", "not_null_columns"):
        if key in payload:
            payload[key] = _listify(payload.get(key)) or []
    spec = PipelineSpec.model_validate(payload)
    source = describe_uc_object(spec.source_table)
    destination = describe_uc_object(spec.target_table)
    if source["checked"] and source["exists"] is False:
        return {
            "result": {
                "error": f"Source {source['name']} is not in Unity Catalog. Choose an existing table or volume file.",
                "code": "source_missing",
                "source": source,
            },
            "artifacts": [],
        }
    destination_missing = destination["checked"] and destination["exists"] is False
    destination_unknown = not destination["checked"]
    if (destination_missing or destination_unknown) and not spec.confirm_create_destination:
        if destination_missing:
            message = (
                f"{destination['name']} is not in Unity Catalog. Create it?"
            )
        else:
            message = f"I could not verify {spec.target_table}. Create it if it is missing?"
        return _confirmation(
            spec.target_table,
            message,
            kind=destination.get("kind") or "table",
            source=spec.source_table,
        )
    title = f"{spec.source_table} → {spec.target_table}"
    try:
        source_columns, target_columns, column_warnings = _columns_for_copy(spec.source_table, spec.target_table)
        request = _validation_request(spec)
        steps, drift, plan_warnings, pairs = build_validation_plan(request, source_columns, target_columns)
    except DataflowError as exc:
        notebook = render_pipeline_notebook(spec)
        return {
            "result": {
                "error": exc.message,
                "code": exc.code,
                "notebook_preview": _truncate(notebook),
                "message": "The notebook is ready, but the copy did not start.",
            },
            "artifacts": [
                {"kind": "code", "title": title, "language": "python", "content": notebook},
            ],
        }
    spec.confirm_create_destination = True
    from app.agent.memory import observe_episode, recall

    from app.agent.skills import quality_activation

    column_names = [column.name for column in source_columns]
    learned = recall(column_names)
    activated = quality_activation()
    notebook = render_pipeline_notebook(spec, steps, columns=column_names, learned=learned)
    code_artifact = {
        "kind": "code",
        "title": title,
        "language": "python",
        "content": notebook,
        "sql": [step.model_dump() for step in steps],
    }
    try:
        status = _run_copy(spec, notebook)
    except DataflowError as exc:
        return {
            "result": {
                "error": exc.message,
                "code": exc.code,
                "notebook_preview": _truncate(notebook),
                "message": "The notebook is ready, but Databricks did not start the copy.",
            },
            "artifacts": [code_artifact],
        }
    payload = status.model_dump()
    artifacts: list[dict[str, Any]] = [
        code_artifact,
        {"kind": "execution", "title": f"Run {status.run_id}", "execution": payload},
    ]
    parsed = _parse_notebook_payload(status.output)
    failed = (not status.finished) or (status.result_state not in {None, "SUCCESS"})
    if parsed.get("status") == "confirmation_required":
        failed = True
    result: dict[str, Any] = {
        "notebook_preview": _truncate(notebook),
        "parameters": pipeline_parameters(spec),
        "write_mode": spec.write_mode,
        "source": source,
        "destination": destination,
        "run": payload,
        "copied": not failed,
        "warnings": [*column_warnings, *plan_warnings],
        "lessons": learned,
        "skills": activated["names"],
    }
    if failed:
        result["error"] = status.error or status.state_message or parsed.get("message") or "Databricks did not finish the copy."
        result["code"] = "copy_failed"
        result["message"] = "The copy did not finish. The notebook is in the code panel."
        return {"result": result, "artifacts": artifacts}

    report = None
    if isinstance(parsed.get("results"), dict):
        dqx_checks = build_dqx_checks(
            primary_keys=spec.primary_keys,
            not_null_columns=spec.not_null_columns or spec.primary_keys,
            formats=spec.formats,
            json_columns=spec.json_columns,
            compare_columns=[column.name for column, _pair in pairs],
            columns=[column.name for column in source_columns],
        )
        report = assemble_report(
            request,
            results=parsed["results"],
            step_errors={key: str(value) for key, value in (parsed.get("errors") or {}).items()},
            drift=drift,
            warnings=[*column_warnings, *plan_warnings],
            dqx_checks=dqx_checks,
            duplicate_keys_collapsed=True,
        )
        report_payload = report.model_dump()
        artifacts.append({"kind": "validation", "title": "Validation report", "report": report_payload, "warnings": report_payload["warnings"]})
        result["report"] = {
            **report_payload,
            "mismatched_keys": report_payload["mismatched_keys"][:20],
            "quarantine_preview": report_payload["quarantine_preview"][:20],
        }
        result["message"] = f"Copied the data and validated it. Status {report.status}."
    else:
        result["message"] = "Copied the data. The notebook finished without a validation payload."
    copy = parsed.get("copy") if isinstance(parsed.get("copy"), dict) else {}
    if copy:
        result["valid_rows"] = copy.get("valid_rows")
        result["quarantine_rows"] = copy.get("quarantine_rows")
    dqx = parsed.get("dqx") if isinstance(parsed.get("dqx"), list) else []
    if dqx:
        result["dqx"] = dqx
        lines = [
            f"{item.get('name')}: {item.get('count')} rows ({item.get('criticality')})"
            + (f" — {', '.join(item.get('samples') or [])}" if item.get("samples") else "")
            for item in dqx
            if isinstance(item, dict)
        ]
        result["dqx_summary"] = lines
        if isinstance(result.get("report"), dict):
            report_warnings = list(result["report"].get("warnings") or [])
            report_warnings.extend(lines)
            result["report"]["warnings"] = report_warnings
        result["message"] = (result.get("message") or "Copied the data.") + " DQX findings: " + "; ".join(lines)
        for artifact in artifacts:
            if artifact.get("kind") != "validation":
                continue
            artifact_warnings = list(artifact.get("warnings") or [])
            artifact_warnings.extend(lines)
            artifact["warnings"] = artifact_warnings
            if isinstance(artifact.get("report"), dict):
                report_warnings = list(artifact["report"].get("warnings") or [])
                report_warnings.extend(lines)
                artifact["report"]["warnings"] = report_warnings
    failing = [str(item.get("name")) for item in dqx if isinstance(item, dict) and item.get("name")]
    observe_episode(
        source=spec.source_table,
        destination=spec.target_table,
        columns=column_names,
        primary_keys=list(spec.primary_keys),
        write_mode=spec.write_mode,
        failing_checks=failing,
    )
    return {"result": result, "artifacts": artifacts}


def _validate(arguments: dict[str, Any]) -> dict[str, Any]:
    payload = dict(arguments)
    for key in (
        "primary_keys",
        "compare_columns",
        "not_null_columns",
        "json_columns",
        "aggregate_dimensions",
        "aggregate_measures",
        "exclude_columns",
    ):
        if key in payload and payload[key] is not None:
            payload[key] = _listify(payload.get(key))
    request = ValidationRequest.model_validate(payload)
    outcome = run_validation(request)
    notebook = render_validation_notebook(request, outcome.sql_steps) if request.include_notebook else None
    outcome.notebook = notebook
    report = outcome.report.model_dump() if outcome.report else None
    result: dict[str, Any] = {
        "executed": request.execute,
        "warnings": outcome.warnings,
        "schema_drift": outcome.schema_drift.model_dump(),
        "dqx_checks": outcome.dqx_checks,
        "sql_step_names": [step.name for step in outcome.sql_steps],
        "report": report,
    }
    if report:
        result["report"] = {
            **report,
            "mismatched_keys": report["mismatched_keys"][:20],
            "quarantine_preview": report["quarantine_preview"][:20],
        }
    artifacts: list[dict[str, Any]] = []
    if notebook:
        artifacts.append(
            {
                "kind": "code",
                "title": "Source-to-target validation",
                "language": "python",
                "content": notebook,
                "sql": [step.model_dump() for step in outcome.sql_steps],
            }
        )
    if report:
        artifacts.append({"kind": "validation", "title": "Validation report", "report": report, "warnings": outcome.warnings})
        artifacts.append(
            {
                "kind": "quarantine",
                "title": "Quarantine preview",
                "report": report,
            }
        )
    return {"result": result, "artifacts": artifacts}


def execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        if name == "search_catalog":
            from app.databricks.search import search_catalog

            found = search_catalog(
                arguments["query"],
                catalog=arguments.get("catalog") or None,
                schema_name=arguments.get("schema_name") or None,
            )
            return {"result": found, "artifacts": []}
        if name == "inspect_uc_object":
            return {"result": describe_uc_object(arguments["name"]), "artifacts": []}
        if name == "create_destination":
            if not arguments.get("confirm"):
                return _confirmation(
                    arguments["target_table"],
                    f"{arguments['target_table']} needs a yes before it can be created. Create it?",
                    kind="table",
                    source=arguments["source_table"],
                )
            from app.databricks.destination import create_missing_destination

            created = create_missing_destination(arguments["source_table"], arguments["target_table"])
            return {"result": created, "artifacts": []}
        if name == "list_tables":
            rows = list_tables(arguments["catalog"], arguments.get("schema_name") or arguments.get("schema"))
            return {"result": {"tables": rows[:200], "count": len(rows)}, "artifacts": []}
        if name == "describe_table":
            columns = get_columns(arguments["table"])
            return {
                "result": {"table": arguments["table"], "columns": [column.model_dump() for column in columns]},
                "artifacts": [],
            }
        if name == "profile_table":
            profile = profile_table(arguments["table"], arguments.get("timestamp_column") or "domain_published_ts")
            return {"result": profile, "artifacts": []}
        if name == "generate_pipeline":
            return _generate(arguments)
        if name == "validate_source_to_target":
            return _validate(arguments)
        if name == "execute_notebook":
            if not arguments.get("confirm"):
                return {
                    "result": {
                        "error": "Refusing to run. Call again with confirm=true only after the user asks to execute.",
                        "code": "confirmation_required",
                    },
                    "artifacts": [],
                }
            status = execute_notebook(
                arguments["code"],
                notebook_name=arguments.get("notebook_name"),
                base_parameters=arguments.get("base_parameters") or {},
                wait=bool(arguments.get("wait", True)),
                timeout_seconds=int(arguments.get("timeout_seconds") or 180),
            )
            payload = status.model_dump()
            return {
                "result": payload,
                "artifacts": [{"kind": "execution", "title": f"Run {status.run_id}", "execution": payload}],
            }
        if name == "load_skill":
            from app.agent.skills import load_skill

            return {"result": load_skill(str(arguments.get("name") or "")), "artifacts": []}
        if name == "recall_memory":
            from app.agent.memory import recall

            lessons = recall(arguments.get("columns") or [])
            return {"result": {"lessons": lessons, "count": len(lessons)}, "artifacts": []}
        if name == "remember_lesson":
            from app.agent.memory import remember

            lesson = remember(
                kind=arguments["kind"],
                column=arguments.get("column") or "",
                detail=arguments.get("detail") or "",
                columns=arguments.get("columns") or [],
                origin="user",
            )
            return {
                "result": {
                    "saved": True,
                    "weight": lesson["weight"],
                    "kind": lesson["kind"],
                    "column": lesson["column"],
                    "message": "Lesson saved. Repeating it makes the next similar source follow it more strongly.",
                },
                "artifacts": [],
            }
        if name == "get_job_run_status":
            status = get_run_status(int(arguments["run_id"]))
            payload = status.model_dump()
            return {
                "result": payload,
                "artifacts": [{"kind": "execution", "title": f"Run {status.run_id}", "execution": payload}],
            }
        return {"result": {"error": f"Unknown tool {name}", "code": "unknown_tool"}, "artifacts": []}
    except ValidationError as exc:
        return {"result": _model_error(exc), "artifacts": []}
    except DataflowError as exc:
        return {"result": {"error": exc.message, "code": exc.code}, "artifacts": []}
    except Exception as exc:  # pragma: no cover - defensive tool boundary
        return {"result": {"error": str(exc), "code": "tool_failed"}, "artifacts": []}


def tool_result_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=str)


_LOCATION_ARGUMENTS = {
    "generate_pipeline": ("source_table", "target_table"),
    "validate_source_to_target": ("source_table", "target_table"),
    "create_destination": ("source_table", "target_table"),
}


def user_locations(messages: list[dict[str, Any]]) -> list[str]:
    """Source then destination from the latest user message that names both."""
    for message in reversed(messages):
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            continue
        found = locations_in_text(content)
        if len(found) >= 2:
            return found[:2]
    return []


def bind_user_locations(name: str, arguments: dict[str, Any], messages: list[dict[str, Any]]) -> dict[str, Any]:
    """Keep tool calls on the source and destination the user wrote."""
    keys = _LOCATION_ARGUMENTS.get(name)
    pair = user_locations(messages)
    if not keys or len(pair) < 2:
        return arguments
    bound = dict(arguments)
    bound[keys[0]] = pair[0]
    bound[keys[1]] = pair[1]
    compare = bound.get("compare_columns")
    if compare is not None and not compare:
        bound["compare_columns"] = None
    return bound
