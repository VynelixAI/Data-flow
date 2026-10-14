"""Databricks workspace client, Unity Catalog metadata, and SQL warehouse execution."""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from typing import Any

from app.databricks.identifiers import parse_uc_location, require_ident, sql_relation, uc_namespace
from app.errors import DataflowError
from app.schemas import ColumnSpec

_ENV_HOST = "DATABRICKS_HOST"
_ENV_TOKEN = "DATABRICKS_TOKEN"
_ENV_WAREHOUSE = "DATABRICKS_WAREHOUSE_ID"


def env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def databricks_host() -> str | None:
    return env(_ENV_HOST)


def require_host_and_token() -> tuple[str, str]:
    host = env(_ENV_HOST)
    token = env(_ENV_TOKEN)
    if not host or not token:
        raise DataflowError(
            "Set DATABRICKS_HOST and DATABRICKS_TOKEN before calling Databricks.",
            code="databricks_not_configured",
            status=503,
        )
    return host, token


def warehouse_id() -> str:
    warehouse = env(_ENV_WAREHOUSE)
    if not warehouse:
        raise DataflowError(
            "Set DATABRICKS_WAREHOUSE_ID to a SQL warehouse. "
            "Validation queries, profiling, and CREATE TABLE run there.",
            code="warehouse_not_configured",
            status=503,
        )
    marker = "/warehouses/"
    if marker in warehouse:
        warehouse = warehouse.split(marker, 1)[1].strip().strip("/")
    return warehouse


def get_workspace_client():
    require_host_and_token()
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as exc:
        raise DataflowError(
            "databricks-sdk is not installed.",
            code="missing_dependency",
            status=500,
        ) from exc
    return WorkspaceClient(host=env(_ENV_HOST), token=env(_ENV_TOKEN))


def list_tables(catalog: str, schema: str) -> list[dict[str, str]]:
    require_ident(catalog, what="catalog")
    require_ident(schema, what="schema")
    client = get_workspace_client()
    rows: list[dict[str, str]] = []
    try:
        tables = client.tables.list(catalog_name=catalog, schema_name=schema, include_browse=True)
        for table in tables:
            rows.append(
                {
                    "name": table.name or "",
                    "full_name": table.full_name or f"{catalog}.{schema}.{table.name}",
                    "table_type": str(table.table_type or ""),
                }
            )
    except DataflowError:
        raise
    except Exception as exc:
        raise DataflowError(
            f"Could not list tables in {catalog}.{schema}: {exc}",
            code="catalog_list_failed",
            status=502,
        ) from exc
    return rows


_SKIPPED_CATALOGS = {"system", "samples", "hive_metastore"}


def list_catalog_names() -> list[str]:
    client = get_workspace_client()
    names: list[str] = []
    try:
        for catalog in client.catalogs.list():
            if catalog.name:
                names.append(catalog.name)
    except DataflowError:
        raise
    except Exception as exc:
        raise DataflowError(f"Could not list catalogs: {exc}", code="catalog_list_failed", status=502) from exc
    return names


def list_schema_names(catalog: str) -> list[str]:
    require_ident(catalog, what="catalog")
    client = get_workspace_client()
    names: list[str] = []
    try:
        for schema in client.schemas.list(catalog_name=catalog):
            if schema.name:
                names.append(schema.name)
    except DataflowError:
        raise
    except Exception as exc:
        raise DataflowError(
            f"Could not list schemas in {catalog}: {exc}",
            code="catalog_list_failed",
            status=502,
        ) from exc
    return names


def resolve_upload_catalog(requested: str | None = None) -> str:
    """Pick a catalog that exists. Never assume `main` is present."""
    names = list_catalog_names()
    if requested:
        chosen = require_ident(requested, what="catalog")
        if chosen not in names:
            available = ", ".join(names) or "none"
            raise DataflowError(
                f"Catalog '{chosen}' does not exist. Available catalogs: {available}.",
                code="catalog_missing",
            )
        return chosen
    configured = env("DATABRICKS_CATALOG")
    if configured and configured in names:
        return require_ident(configured, what="catalog")
    if "workspace" in names:
        return "workspace"
    for name in names:
        if name not in _SKIPPED_CATALOGS:
            return require_ident(name, what="catalog")
    available = ", ".join(names) or "none"
    raise DataflowError(
        f"No writable Unity Catalog was found. Available catalogs: {available}. "
        "Set DATABRICKS_CATALOG to a catalog that exists.",
        code="catalog_missing",
    )


def _is_absent(exc: Exception) -> bool:
    text = str(exc).lower()
    return type(exc).__name__ == "NotFound" or any(
        token in text for token in ("not found", "does not exist", "404", "resource_does_not_exist")
    )


def volume_file_exists(path: str) -> bool:
    """True when a Unity Catalog volume file or directory is present.

    The SDK dbutils helper has no exists() method, so this uses the Files API.
    """
    client = get_workspace_client()
    try:
        client.files.get_metadata(path)
        return True
    except Exception as exc:
        if not _is_absent(exc):
            raise DataflowError(f"Could not check {path}: {exc}", code="uc_lookup_failed", status=502) from exc
    try:
        client.files.get_directory_metadata(path)
        return True
    except Exception as exc:
        if _is_absent(exc):
            return False
        raise DataflowError(f"Could not check {path}: {exc}", code="uc_lookup_failed", status=502) from exc


def describe_uc_object(name: str) -> dict[str, Any]:
    """Return whether a Unity Catalog table or volume file is present. Never creates it."""
    location = parse_uc_location(name)
    result: dict[str, Any] = {
        "name": location.name,
        "kind": location.kind,
        "format": location.format,
        "exists": None,
        "checked": False,
    }
    try:
        client = get_workspace_client()
    except DataflowError as exc:
        result["error"] = exc.message
        return result
    if location.kind == "table":
        try:
            client.tables.get(full_name=location.name)
            result["exists"] = True
        except Exception as exc:
            if _is_absent(exc):
                result["exists"] = False
            else:
                raise DataflowError(str(exc), code="uc_lookup_failed", status=502) from exc
    else:
        result["exists"] = volume_file_exists(location.name)
    result["checked"] = True
    return result


def get_columns(full_name: str) -> list[ColumnSpec]:
    location = parse_uc_location(full_name)
    if location.is_file:
        described = run_sql(
            f"DESCRIBE QUERY SELECT * FROM {sql_relation(location)}",
            catalog=uc_namespace(location)[0],
            schema=uc_namespace(location)[1],
        )
        columns: list[ColumnSpec] = []
        for row in described:
            name = str(row.get("col_name") or row.get("column_name") or "").strip()
            if not name or name.startswith("#"):
                continue
            columns.append(ColumnSpec(name=name, data_type=str(row.get("data_type") or "string")))
        if not columns:
            raise DataflowError(f"{full_name} has no columns.", code="empty_schema")
        return columns
    client = get_workspace_client()
    table = client.tables.get(full_name=location.name)
    columns: list[ColumnSpec] = []
    for column in table.columns or []:
        data_type = column.type_text or (column.type_name.value if column.type_name else None) or "string"
        columns.append(
            ColumnSpec(
                name=column.name or "",
                data_type=str(data_type),
                nullable=True if column.nullable is None else bool(column.nullable),
            )
        )
    if not columns:
        raise DataflowError(f"Table {full_name} has no columns.", code="empty_schema")
    return columns


def _cell(value: Any) -> Any:
    if isinstance(value, dict) and "string_value" in value:
        return value.get("string_value")
    if isinstance(value, dict) and len(value) == 1:
        return next(iter(value.values()))
    return value


def _rows_from_chunk(columns: list[str], data_array: list[list[Any]] | None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in data_array or []:
        rows.append({columns[index]: _cell(raw[index]) if index < len(raw) else None for index in range(len(columns))})
    return rows


def run_sql(statement: str, *, catalog: str | None = None, schema: str | None = None, timeout_seconds: int | None = None) -> list[dict[str, Any]]:
    """Execute one read statement on the configured SQL warehouse and return rows."""
    client = get_workspace_client()
    from databricks.sdk.service.sql import Disposition, Format, StatementState

    timeout = timeout_seconds or int(env("DATABRICKS_SQL_TIMEOUT", "180") or "180")
    response = client.statement_execution.execute_statement(
        warehouse_id=warehouse_id(),
        statement=statement,
        catalog=catalog,
        schema=schema,
        wait_timeout="50s",
        disposition=Disposition.INLINE,
        format=Format.JSON_ARRAY,
    )
    deadline = time.time() + timeout
    while response.status is not None and response.status.state in {StatementState.PENDING, StatementState.RUNNING}:
        if time.time() > deadline:
            if response.statement_id:
                client.statement_execution.cancel_execution(statement_id=response.statement_id)
            raise DataflowError("SQL warehouse statement timed out.", code="sql_timeout", status=504)
        time.sleep(1.5)
        response = client.statement_execution.get_statement(statement_id=response.statement_id)

    state = response.status.state if response.status else None
    if state != StatementState.SUCCEEDED:
        message = ""
        if response.status and response.status.error:
            message = response.status.error.message or str(response.status.error)
        raise DataflowError(message or f"SQL statement failed ({state}).", code="sql_failed")

    manifest_columns = []
    if response.manifest and response.manifest.schema and response.manifest.schema.columns:
        manifest_columns = [column.name or f"c{index}" for index, column in enumerate(response.manifest.schema.columns)]

    rows: list[dict[str, Any]] = []
    first = response.result.data_array if response.result else None
    rows.extend(_rows_from_chunk(manifest_columns, first))

    next_index = None
    if response.result is not None:
        next_index = getattr(response.result, "next_chunk_index", None)
    while next_index is not None and response.statement_id:
        chunk = client.statement_execution.get_statement_result_chunk_n(
            statement_id=response.statement_id,
            chunk_index=next_index,
        )
        rows.extend(_rows_from_chunk(manifest_columns, chunk.data_array))
        next_index = getattr(chunk, "next_chunk_index", None)
    return rows


def default_catalog() -> str:
    return require_ident(env("DATABRICKS_CATALOG", "main") or "main", what="catalog")


def default_schema() -> str:
    return require_ident(env("DATABRICKS_SCHEMA", "dataflow") or "dataflow", what="schema")


def default_volume() -> str:
    return require_ident(env("DATABRICKS_VOLUME", "uploads") or "uploads", what="volume")


def notebook_dir() -> str:
    path = env("DATABRICKS_NOTEBOOK_DIR", "/Workspace/Shared/dataflow-agent") or "/Workspace/Shared/dataflow-agent"
    if not path.startswith("/"):
        raise DataflowError("DATABRICKS_NOTEBOOK_DIR must be an absolute workspace path.", code="invalid_path")
    if ".." in path or "//" in path:
        raise DataflowError("DATABRICKS_NOTEBOOK_DIR is not a safe path.", code="invalid_path")
    return path.rstrip("/")


def iter_health() -> Iterator[tuple[str, bool]]:
    yield "databricks_host_configured", bool(env(_ENV_HOST))
    yield "databricks_token_configured", bool(env(_ENV_TOKEN))
    yield "databricks_warehouse_configured", bool(env(_ENV_WAREHOUSE))
    yield "openai_configured", bool(env("OPENAI_API_KEY"))


def list_volume_files(
    catalog: str,
    schema: str,
    volume: str,
) -> list[dict[str, Any]]:
    """List files in a Unity Catalog volume without changing its contents."""
    require_ident(catalog, what="catalog")
    require_ident(schema, what="schema")
    require_ident(volume, what="volume")

    directory = f"/Volumes/{catalog}/{schema}/{volume}"
    client = get_workspace_client()
    rows: list[dict[str, Any]] = []

    try:
        for item in client.files.list_directory_contents(
            directory_path=directory
        ):
            path = item.path or ""
            name = item.name or path.rsplit("/", 1)[-1]

            rows.append({
                "name": name,
                "path": path,
                "is_directory": bool(item.is_directory),
                "size": item.file_size,
            })

    except Exception as exc:
        raise DataflowError(
            f"Could not list files in {directory}: {exc}",
            code="volume_list_failed",
            status=502,
        ) from exc

    return rows