"""Upload CSV or Excel into a Unity Catalog Volume and create a Delta table."""

from __future__ import annotations

import io
import json
import re
from datetime import datetime, timezone

from app.databricks.client import (
    default_schema,
    default_volume,
    env,
    get_columns,
    get_workspace_client,
    resolve_upload_catalog,
    run_sql,
)
from app.databricks.identifiers import quote_fqn, require_ident, safe_filename, safe_upload_stem, volume_uri
from app.errors import DataflowError
from app.schemas import UploadResult

_MAX_BYTES = 50 * 1024 * 1024
_EXCEL = {"xlsx", "xlsm", "xls"}
_CSV = {"csv", "txt", "tsv"}


def _extension(filename: str) -> str:
    if "." not in filename:
        return ""
    return filename.rsplit(".", 1)[-1].lower()


def _unique_headers(names: list[str]) -> list[str]:
    seen: dict[str, int] = {}
    unique: list[str] = []
    for name in names:
        count = seen.get(name, 0) + 1
        seen[name] = count
        unique.append(name if count == 1 else f"{name}_{count}")
    return unique


def _frame_to_csv(frame, sheet: str | None) -> tuple[bytes, str | None, bool]:
    if frame.empty and len(frame.columns) == 0:
        raise DataflowError("The uploaded file has no columns.", code="empty_file")
    frame = frame.copy()
    frame.columns = _unique_headers(
        [_sanitize_header(str(column), index) for index, column in enumerate(frame.columns)]
    )
    has_timestamp = any(str(column).lower() == "domain_published_ts" for column in frame.columns)
    buffer = io.StringIO()
    frame.to_csv(buffer, index=False)
    return buffer.getvalue().encode("utf-8"), sheet, has_timestamp


def _to_csv_bytes(content: bytes, filename: str, sheet: str | None) -> tuple[bytes, str | None, bool]:
    extension = _extension(filename)
    try:
        import pandas as pd
    except ImportError as exc:  # pragma: no cover
        raise DataflowError("pandas is required to read uploads.", code="missing_dependency", status=500) from exc
    if extension in _CSV or extension == "":
        if not content.strip():
            raise DataflowError("The uploaded file is empty.", code="empty_file")
        try:
            frame = pd.read_csv(io.BytesIO(content))
        except Exception as exc:
            raise DataflowError(f"Could not read the CSV file: {exc}", code="invalid_csv") from exc
        return _frame_to_csv(frame, None)
    if extension not in _EXCEL:
        raise DataflowError("Upload a CSV or Excel file (.csv, .xlsx).", code="unsupported_file")
    try:
        workbook = pd.ExcelFile(io.BytesIO(content))
    except Exception as exc:
        raise DataflowError(f"Could not read the Excel file: {exc}", code="invalid_excel") from exc
    if not workbook.sheet_names:
        raise DataflowError("The workbook has no sheets.", code="empty_file")
    chosen = sheet or workbook.sheet_names[0]
    if chosen not in workbook.sheet_names:
        raise DataflowError(
            f"Sheet '{chosen}' was not found. Available: {', '.join(workbook.sheet_names)}.",
            code="missing_sheet",
        )
    return _frame_to_csv(workbook.parse(chosen), chosen)


def _sanitize_header(name: str, index: int) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", name.strip()).strip("_").lower()
    cleaned = re.sub(r"_+", "_", cleaned)
    if not cleaned or cleaned.lower().startswith("unnamed"):
        cleaned = f"column_{index + 1}"
    if cleaned[0].isdigit():
        cleaned = f"c_{cleaned}"
    return cleaned[:80]


def _ensure_volume(catalog: str, schema: str, volume: str) -> None:
    client = get_workspace_client()
    from databricks.sdk.service.catalog import VolumeType

    try:
        client.volumes.read(name=f"{catalog}.{schema}.{volume}")
        return
    except Exception:
        pass
    try:
        client.volumes.create(
            catalog_name=catalog,
            schema_name=schema,
            name=volume,
            volume_type=VolumeType.MANAGED,
            comment="DataFlow Agent uploads",
        )
    except Exception as exc:
        message = str(exc)
        if "already exists" in message.lower():
            return
        raise DataflowError(
            f"Could not create volume {catalog}.{schema}.{volume}: {message}",
            code="volume_create_failed",
            status=502,
        ) from exc


def _ensure_schema(catalog: str, schema: str) -> None:
    client = get_workspace_client()
    try:
        client.schemas.get(full_name=f"{catalog}.{schema}")
        return
    except Exception:
        pass
    try:
        client.schemas.create(name=schema, catalog_name=catalog, comment="DataFlow Agent")
    except Exception as exc:
        if "already exists" in str(exc).lower():
            return
        raise DataflowError(
            f"Could not create schema {catalog}.{schema}: {exc}",
            code="schema_create_failed",
            status=502,
        ) from exc


def _materialize_table(create_sql: str, full_name: str, catalog: str, schema: str) -> int | None:
    """Create the Delta table on the SQL warehouse, or on serverless when no warehouse is set."""
    if env("DATABRICKS_WAREHOUSE_ID"):
        run_sql(create_sql, catalog=catalog, schema=schema)
        counts = run_sql(
            f"SELECT COUNT(*) AS row_count FROM {quote_fqn(full_name)}",
            catalog=catalog,
            schema=schema,
        )
        return int(float(counts[0]["row_count"])) if counts else None

    from app.databricks.notebook_executor import execute_notebook

    code = f"""
import json
spark.sql({json.dumps(create_sql)})
count = spark.table({json.dumps(full_name)}).count()
dbutils.notebook.exit(json.dumps({{"row_count": int(count)}}))
"""
    status = execute_notebook(code, notebook_name="dataflow_upload", wait=True, timeout_seconds=600)
    text = (status.output or "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict) and parsed.get("row_count") is not None:
            return int(parsed["row_count"])
    if status.result_state not in {None, "SUCCESS"}:
        raise DataflowError(
            status.error or status.state_message or "Upload notebook did not create the Delta table.",
            code="upload_failed",
            status=502,
        )
    return None


def upload_and_create_table(
    content: bytes,
    filename: str,
    *,
    catalog: str | None = None,
    schema: str | None = None,
    volume: str | None = None,
    table_name: str | None = None,
    sheet: str | None = None,
    replace: bool = True,
) -> UploadResult:
    if not content:
        raise DataflowError("The uploaded file is empty.", code="empty_file")
    if len(content) > _MAX_BYTES:
        raise DataflowError("File exceeds the 50 MB upload limit.", code="file_too_large")

    catalog_name = resolve_upload_catalog(catalog)
    schema_name = require_ident(schema or default_schema(), what="schema")
    volume_name = require_ident(volume or default_volume(), what="volume")
    csv_bytes, chosen_sheet, has_timestamp = _to_csv_bytes(content, filename, sheet)
    stored_name = safe_filename(filename, default_ext="csv")
    if not stored_name.lower().endswith(".csv"):
        stored_name = f"{safe_upload_stem(filename)}.csv"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = volume_uri(catalog_name, schema_name, volume_name, "uploads", stamp, stored_name)
    table = table_name or f"{safe_upload_stem(filename)}_{stamp[:8]}"
    require_ident(table, what="table")
    full_name = f"{catalog_name}.{schema_name}.{table}"

    _ensure_schema(catalog_name, schema_name)
    _ensure_volume(catalog_name, schema_name, volume_name)
    client = get_workspace_client()
    try:
        client.files.upload(destination, io.BytesIO(csv_bytes), overwrite=True)
    except Exception as exc:
        raise DataflowError(f"Volume upload failed: {exc}", code="upload_failed", status=502) from exc

    verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE IF NOT EXISTS"
    published = "" if has_timestamp else "  current_timestamp() AS domain_published_ts,\n"
    # read_files infers types. A published timestamp is added when the file does not
    # already carry one, so the new table can be reconciled immediately.
    create_sql = f"""
{verb} {quote_fqn(full_name)}
USING DELTA
AS
SELECT
{published}  src.*,
  current_timestamp() AS _dataflow_ingested_at
FROM read_files(
  '{destination}',
  format => 'csv',
  header => true,
  inferSchema => true
) src
""".strip()
    row_count = _materialize_table(create_sql, full_name, catalog_name, schema_name)

    columns = get_columns(full_name)
    added_timestamp = any(column.name.lower() == "domain_published_ts" for column in columns)
    return UploadResult(
        table=full_name,
        volume_path=destination,
        columns=columns,
        row_count=row_count,
        sheet=chosen_sheet,
        original_filename=filename,
        added_timestamp_column=added_timestamp,
    )
