"""Strict identifier quoting so generated Spark SQL cannot be injected into."""

from __future__ import annotations

import math
import re

from app.errors import DataflowError

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})?$"
)
_SPARK_TYPE = re.compile(r"^[A-Za-z0-9_<>(),: \n]+$")
_VOLUME_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*$")


def require_ident(name: str, *, what: str = "identifier") -> str:
    if not isinstance(name, str) or not _IDENT.match(name):
        raise DataflowError(
            f"Invalid {what} '{name}'. Use letters, numbers, and underscores, starting with a letter or underscore.",
            code="invalid_identifier",
        )
    return name


def quote_ident(name: str) -> str:
    require_ident(name)
    return f"`{name}`"


def parse_fqn(fqn: str) -> tuple[str, str, str]:
    parts = (fqn or "").split(".")
    if len(parts) != 3:
        raise DataflowError(
            f"Expected a fully qualified name catalog.schema.table, got '{fqn}'.",
            code="invalid_table_name",
        )
    catalog, schema, table = (require_ident(part, what="name part") for part in parts)
    return catalog, schema, table


def quote_fqn(fqn: str) -> str:
    return ".".join(quote_ident(part) for part in parse_fqn(fqn))


class UcLocation:
    """A Unity Catalog table or a CSV/Excel file in a volume."""

    def __init__(self, kind: str, name: str, format: str | None = None):
        self.kind = kind
        self.name = name
        self.format = format

    @property
    def is_file(self) -> bool:
        return self.kind == "volume_file"


def parse_uc_location(value: str) -> UcLocation:
    raw = (value or "").strip()
    if raw.startswith("/Volumes/"):
        parts = [part for part in raw.split("/") if part != ""]
        # Volumes, catalog, schema, volume, filename...
        if len(parts) < 5 or parts[0] != "Volumes":
            raise DataflowError(
                "Volume files must look like /Volumes/catalog/schema/volume/file.csv.",
                code="invalid_path",
            )
        for segment in parts[1:]:
            if segment in {".", ".."} or not _VOLUME_SEGMENT.match(segment):
                raise DataflowError(f"Unsafe volume path '{raw}'.", code="invalid_path")
        filename = parts[-1].lower()
        if filename.endswith((".csv", ".txt", ".tsv")):
            file_format = "csv"
        elif filename.endswith((".xlsx", ".xlsm", ".xls")):
            file_format = "excel"
        else:
            raise DataflowError(
                "Unity Catalog volume files must be CSV or Excel (.csv, .xlsx).",
                code="unsupported_file",
            )
        return UcLocation("volume_file", "/Volumes/" + "/".join(parts[1:]), file_format)
    catalog, schema, table = parse_fqn(raw)
    return UcLocation("table", f"{catalog}.{schema}.{table}")


def uc_namespace(location: UcLocation) -> tuple[str, str]:
    if location.kind == "table":
        catalog, schema, _table = parse_fqn(location.name)
        return catalog, schema
    parts = location.name.split("/")
    return parts[2], parts[3]


def sql_relation(value: str | UcLocation) -> str:
    """SQL fragment that reads a Unity Catalog table or volume file."""
    location = value if isinstance(value, UcLocation) else parse_uc_location(value)
    if location.kind == "table":
        return quote_fqn(location.name)
    if "'" in location.name:
        raise DataflowError("Unsafe volume path.", code="invalid_path")
    file_format = "csv" if location.format == "csv" else "excel"
    return (
        f"read_files('{location.name}', format => '{file_format}', header => true, inferSchema => true)"
    )


def sql_string_literal(value: str) -> str:
    """Quote a value that has already passed a strict timestamp or format check."""
    if "'" in value or "\\" in value or "\n" in value:
        raise DataflowError("Unsafe literal.", code="invalid_literal")
    return f"'{value}'"


def validate_timestamp_literal(value: str) -> str:
    if not _TIMESTAMP.match(value or ""):
        raise DataflowError(
            f"Timestamp '{value}' must look like 2026-04-01 12:30:00.",
            code="invalid_timestamp",
        )
    return value.replace("T", " ").replace("Z", "")


def sql_float(value: float) -> str:
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise DataflowError("Tolerance must be a finite number >= 0.", code="invalid_tolerance")
    return format(number, ".12g")


def validate_spark_type(spark_type: str) -> str:
    cleaned = (spark_type or "").strip()
    if not cleaned or not _SPARK_TYPE.match(cleaned) or "--" in cleaned or ";" in cleaned:
        raise DataflowError(f"Unsupported Spark type '{spark_type}'.", code="invalid_spark_type")
    return cleaned


def volume_uri(catalog: str, schema: str, volume: str, *parts: str) -> str:
    segments = [require_ident(catalog), require_ident(schema), require_ident(volume)]
    safe_parts: list[str] = []
    for part in parts:
        if not part or not _VOLUME_SEGMENT.match(part):
            raise DataflowError(f"Unsafe volume path segment '{part}'.", code="invalid_path")
        safe_parts.append(part)
    return "/Volumes/" + "/".join(segments + safe_parts)


def safe_upload_stem(filename: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    stem = base.rsplit(".", 1)[0]
    stem = re.sub(r"[^A-Za-z0-9_]", "_", stem).strip("_").lower()
    stem = re.sub(r"_+", "_", stem)
    if not stem:
        stem = "upload"
    if stem[0].isdigit():
        stem = f"t_{stem}"
    if not _IDENT.match(stem):
        stem = "upload"
    return stem[:80]


_TABLE_IN_TEXT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)\b")
_VOLUME_IN_TEXT = re.compile(
    r"/Volumes/(?:[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*/)+[A-Za-z0-9_\-]+(?:\.[A-Za-z0-9_\-]+)*\.(?:csv|xlsx|xls|xlsm|txt|tsv)",
    re.IGNORECASE,
)


def locations_in_text(text: str) -> list[str]:
    """Unity Catalog tables and volume files, in the order the user wrote them."""
    found: list[tuple[int, str]] = []
    for match in _VOLUME_IN_TEXT.finditer(text or ""):
        found.append((match.start(), match.group(0)))
    for match in _TABLE_IN_TEXT.finditer(text or ""):
        found.append((match.start(), match.group(1)))
    found.sort(key=lambda item: item[0])
    ordered: list[str] = []
    for _, name in found:
        if name not in ordered:
            ordered.append(name)
    return ordered


def safe_filename(filename: str, *, default_ext: str) -> str:
    base = filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = re.sub(r"[^A-Za-z0-9._\-]", "_", base).strip("._")
    if "." not in cleaned:
        cleaned = f"{cleaned or 'upload'}.{default_ext}"
    return cleaned[:180]
