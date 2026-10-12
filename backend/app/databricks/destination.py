"""Create a missing Unity Catalog destination after the operator says yes."""

from __future__ import annotations

import csv
import io
import json

from app.databricks.client import describe_uc_object, env, get_workspace_client, run_sql
from app.databricks.identifiers import UcLocation, parse_fqn, parse_uc_location, quote_fqn, sql_relation
from app.errors import DataflowError


def table_creation_sql(source: str, target: str) -> str:
    """Empty Delta table whose columns match the source. Does not copy rows."""
    destination = parse_uc_location(target)
    if destination.kind != "table":
        raise DataflowError("SQL table creation requires catalog.schema.table.", code="not_a_table")
    relation = sql_relation(source)
    return (
        f"CREATE TABLE IF NOT EXISTS {quote_fqn(destination.name)} "
        f"USING DELTA AS SELECT * FROM {relation} WHERE 1 = 0"
    )


def render_create_notebook(source: str, target: str) -> str:
    """Notebook that creates the destination object and writes zero rows."""
    src = parse_uc_location(source)
    dst = parse_uc_location(target)
    return f"""# Databricks notebook source
# DataFlow Agent creates the destination only after an operator said yes.
source_raw = {json.dumps(src.name)}
target_raw = {json.dumps(dst.name)}
source_kind = {json.dumps(src.kind)}
target_kind = {json.dumps(dst.kind)}

if source_kind == "table":
    frame = spark.table(source_raw).limit(0)
else:
    lower = source_raw.lower()
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        import pandas as pd
        frame = spark.createDataFrame(pd.read_excel(source_raw)).limit(0)
    else:
        frame = spark.read.option("header", True).option("inferSchema", True).csv(source_raw).limit(0)

if target_kind == "table":
    frame.write.format("delta").mode("overwrite").option("overwriteSchema", "true").saveAsTable(target_raw)
else:
    pdf = frame.toPandas()
    lower = target_raw.lower()
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        pdf.to_excel(target_raw, index=False)
    else:
        pdf.to_csv(target_raw, index=False)

dbutils.notebook.exit(target_raw)
"""


def _ensure_schema(catalog: str, schema: str) -> None:
    client = get_workspace_client()
    full_name = f"{catalog}.{schema}"
    try:
        client.schemas.get(full_name=full_name)
        return
    except Exception as exc:
        text = str(exc).lower()
        if not any(token in text for token in ("not found", "does not exist", "404", "resource_does_not_exist")):
            raise DataflowError(str(exc), code="schema_lookup_failed", status=502) from exc
    client.schemas.create(name=schema, catalog_name=catalog)


def header_file_bytes(columns: list[str], file_format: str | None) -> bytes:
    """Header-only CSV or Excel. The pipeline loads rows later."""
    if not columns:
        raise DataflowError("The source has no columns to copy onto the destination file.", code="empty_schema")
    if file_format == "excel":
        import pandas as pd

        buffer = io.BytesIO()
        pd.DataFrame(columns=columns).to_excel(buffer, index=False)
        return buffer.getvalue()
    text = io.StringIO()
    csv.writer(text).writerow(columns)
    return text.getvalue().encode("utf-8")


def _source_columns(source: str) -> list[str]:
    location = parse_uc_location(source)
    client = get_workspace_client()
    if location.kind == "table":
        try:
            table = client.tables.get(full_name=location.name)
        except Exception as exc:
            raise DataflowError(
                f"Could not read columns from {location.name}: {exc}",
                code="source_lookup_failed",
                status=502,
            ) from exc
        names = [column.name for column in (table.columns or []) if column.name]
    else:
        names = _columns_from_volume_file(location)
    if not names:
        raise DataflowError(f"{location.name} has no columns.", code="empty_schema")
    return names


def _columns_from_volume_file(location: UcLocation) -> list[str]:
    import pandas as pd

    client = get_workspace_client()
    try:
        downloaded = client.files.download(location.name)
        raw = downloaded.contents.read() if downloaded.contents is not None else b""
    except Exception as exc:
        raise DataflowError(
            f"Could not read columns from {location.name}: {exc}",
            code="source_lookup_failed",
            status=502,
        ) from exc
    stream = io.BytesIO(raw)
    frame = pd.read_excel(stream, nrows=0) if location.format == "excel" else pd.read_csv(stream, nrows=0)
    return [str(column) for column in frame.columns if str(column).strip()]


def _create_volume_file(source: str, destination: UcLocation) -> None:
    from app.databricks.file_upload import _ensure_volume

    parts = destination.name.split("/")
    catalog, schema, volume = parts[2], parts[3], parts[4]
    _ensure_schema(catalog, schema)
    _ensure_volume(catalog, schema, volume)
    parent = destination.name.rsplit("/", 1)[0]
    client = get_workspace_client()
    if parent.count("/") > 4:
        try:
            client.files.create_directory(parent)
        except Exception as exc:
            if "already exists" not in str(exc).lower():
                raise DataflowError(
                    f"Could not create the folder for {destination.name}: {exc}",
                    code="volume_create_failed",
                    status=502,
                ) from exc
    payload = header_file_bytes(_source_columns(source), destination.format)
    try:
        client.files.upload(destination.name, io.BytesIO(payload), overwrite=False)
    except Exception as exc:
        if "already exists" in str(exc).lower():
            return
        raise DataflowError(
            f"Could not create {destination.name}: {exc}",
            code="create_failed",
            status=502,
        ) from exc


def create_missing_destination(source: str, target: str) -> dict:
    """Create the destination if it is absent. A second call leaves an existing object alone."""
    current = describe_uc_object(target)
    if current.get("error") and not current.get("checked"):
        raise DataflowError(current["error"], code="uc_lookup_failed", status=502)
    if current.get("checked") and current.get("exists"):
        return {"created": False, "exists": True, "name": current["name"], "kind": current["kind"]}

    destination = parse_uc_location(target)
    if destination.kind == "volume_file":
        _create_volume_file(source, destination)
        method = "files"
    elif destination.kind == "table" and env("DATABRICKS_WAREHOUSE_ID"):
        catalog, schema, _table = parse_fqn(destination.name)
        _ensure_schema(catalog, schema)
        run_sql(table_creation_sql(source, target), catalog=catalog, schema=schema)
        method = "sql"
    else:
        from app.databricks.notebook_executor import execute_notebook

        if destination.kind == "table":
            catalog, schema, _table = parse_fqn(destination.name)
            _ensure_schema(catalog, schema)
        status = execute_notebook(
            render_create_notebook(source, target),
            notebook_name="create_destination",
            wait=True,
            timeout_seconds=240,
        )
        if not status.finished:
            raise DataflowError(
                "Destination creation is still running on Databricks. Check the run before writing data.",
                code="create_pending",
                status=504,
            )
        if status.result_state not in {None, "SUCCESS"}:
            raise DataflowError(
                status.error or status.state_message or "Databricks did not create the destination.",
                code="create_failed",
                status=502,
            )
        method = "notebook"

    confirmed = describe_uc_object(target)
    return {
        "created": True,
        "exists": confirmed.get("exists"),
        "name": destination.name,
        "kind": destination.kind,
        "method": method,
    }
