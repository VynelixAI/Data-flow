"""Find Unity Catalog tables and volume files from a partial name.

The walker is bounded so a broad question does not list the whole metastore.
Matching itself is pure and does not call Databricks.
"""

from __future__ import annotations

import re

from app.databricks.client import env, get_workspace_client, run_sql
from app.databricks.identifiers import require_ident
from app.errors import DataflowError

_TOKEN = re.compile(r"[a-z0-9]+")
_DATA_FILE = re.compile(r"\.(csv|txt|tsv|xlsx|xlsm|xls)$", re.IGNORECASE)
_MAX_CATALOGS = 6
_MAX_SCHEMAS = 15
_MAX_VOLUMES = 8
_MAX_EXAMINED = 2000
_MAX_PER_SCHEMA = 250
_MAX_FILE_DEPTH = 2


def match_rank(query: str, candidate: str) -> int:
    """Higher means a closer name. Zero means the candidate should be dropped."""
    needle = (query or "").strip().lower()
    if len(needle) < 2:
        return 0
    haystack = candidate.strip().lower()
    leaf = haystack.rstrip("/").split("/")[-1]
    stem = leaf.rsplit(".", 1)[0] if "." in leaf else leaf
    table_name = haystack.split(".")[-1]
    if needle in {haystack, leaf, stem, table_name}:
        return 100
    if stem.startswith(needle) or table_name.startswith(needle) or leaf.startswith(needle):
        return 80
    if needle in stem or needle in table_name or needle in leaf:
        return 60
    tokens = [token for token in _TOKEN.findall(needle) if len(token) > 1]
    if tokens and all(token in haystack.replace("_", " ") or token in haystack for token in tokens):
        return 40
    if needle in haystack:
        return 30
    return 0


def collect_hits(query: str, objects: list[dict], *, limit: int = 20) -> list[dict]:
    hits: list[dict] = []
    for obj in objects:
        rank = match_rank(query, str(obj.get("name") or ""))
        if not rank:
            continue
        hits.append({**obj, "rank": rank})
    hits.sort(key=lambda item: (-int(item["rank"]), str(item.get("name") or "")))
    return hits[:limit]


def search_catalog(
    query: str,
    *,
    catalog: str | None = None,
    schema_name: str | None = None,
    limit: int = 20,
) -> dict:
    needle = (query or "").strip()
    if len(needle) < 2:
        raise DataflowError("Search needs at least 2 characters.", code="query_too_short")
    if len(needle) > 120:
        raise DataflowError("Search text is too long.", code="query_too_long")
    if catalog:
        require_ident(catalog, what="catalog")
    if schema_name:
        require_ident(schema_name, what="schema")
    limit = max(1, min(int(limit), 40))
    client = get_workspace_client()
    objects: list[dict] = []
    examined = 0
    truncated = False
    sql_tables = False
    if env("DATABRICKS_WAREHOUSE_ID"):
        objects, examined = _search_tables_sql(needle, catalog=catalog, schema_name=schema_name)
        sql_tables = bool(objects)
    catalogs = [catalog] if catalog else _catalog_names(client)
    if not catalogs:
        default = env("DATABRICKS_CATALOG", "main") or "main"
        catalogs = [default]
    for cat in catalogs[:_MAX_CATALOGS]:
        schemas = [schema_name] if schema_name else _ordered_schemas(_schema_names(client, cat), needle)
        if sql_tables:
            known = {item["name"].split(".")[1] for item in objects if item["name"].count(".") == 2}
            preferred = env("DATABRICKS_SCHEMA", "dataflow") or "dataflow"
            schemas = [name for name in schemas if name in known or match_rank(needle, name) or name == preferred]
        preferred_schema = env("DATABRICKS_SCHEMA", "dataflow") or "dataflow"
        for schema in schemas[:_MAX_SCHEMAS]:
            before = len(objects)
            if not sql_tables:
                examined, truncated = _take_tables(client, cat, schema, needle, objects, examined)
                if truncated:
                    break
            # if len(objects) > before or match_rank(needle, schema) or schema == preferred_schema:
                examined, truncated = _take_files(client, cat, schema, needle, objects, examined)
            if truncated:
                break
        if truncated:
            break
    ranked = collect_hits(needle, objects, limit=10_000)
    return {
        "query": needle,
        "matches": ranked[:limit],
        "examined": examined,
        "truncated": truncated or len(ranked) > limit,
    }


def _like_fragment(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_ ]+", "", value.lower()).strip()
    escaped = cleaned.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped


def _search_tables_sql(query: str, *, catalog: str | None, schema_name: str | None) -> tuple[list[dict], int]:
    """Ask the warehouse for table names that contain the words in the query."""
    words = [word for word in _TOKEN.findall(query.lower()) if len(word) > 1]
    if not words:
        return [], 0
    clauses = []
    for word in words[:4]:
        fragment = _like_fragment(word)
        if not fragment:
            continue
        clauses.append(
            "(LOWER(table_name) LIKE '%"
            + fragment
            + "%' ESCAPE '\\' OR LOWER(table_schema) LIKE '%"
            + fragment
            + "%' ESCAPE '\\')"
        )
    if not clauses:
        return [], 0
    where = " AND ".join(clauses)
    if catalog:
        where += f" AND table_catalog = '{catalog}'"
    if schema_name:
        where += f" AND table_schema = '{schema_name}'"
    statement = (
        "SELECT table_catalog, table_schema, table_name, table_type "
        "FROM system.information_schema.tables "
        "WHERE table_catalog NOT IN ('system', 'hive_metastore') AND "
        + where
        + " LIMIT 40"
    )
    try:
        rows = run_sql(statement)
    except DataflowError:
        return [], 0
    objects = []
    for row in rows:
        cat = str(row.get("table_catalog") or "")
        schema = str(row.get("table_schema") or "")
        name = str(row.get("table_name") or "")
        if not (cat and schema and name):
            continue
        objects.append(
            {
                "kind": "table",
                "name": f"{cat}.{schema}.{name}",
                "table_type": str(row.get("table_type") or ""),
            }
        )
    return objects, len(rows)


def _catalog_names(client) -> list[str]:
    names: list[str] = []
    preferred = env("DATABRICKS_CATALOG", "main") or "main"
    try:
        for item in client.catalogs.list(max_results=0):
            name = getattr(item, "name", None)
            if name:
                names.append(name)
    except Exception as exc:
        raise DataflowError(str(exc), code="catalog_search_failed", status=502) from exc
    names.sort(key=lambda name: (name != preferred, name))
    return names


def _schema_names(client, catalog: str) -> list[str]:
    names: list[str] = []
    preferred = env("DATABRICKS_SCHEMA", "dataflow") or "dataflow"
    try:
        for item in client.schemas.list(catalog_name=catalog, max_results=0):
            name = getattr(item, "name", None)
            if name:
                names.append(name)
    except Exception:
        return []
    names.sort(key=lambda name: (name != preferred, name))
    return names


def _ordered_schemas(names: list[str], query: str) -> list[str]:
    return sorted(names, key=lambda name: (-match_rank(query, name), name))


def _take_tables(
    client, catalog: str, schema: str, query: str, objects: list[dict], examined: int
) -> tuple[int, bool]:
    try:
        tables = client.tables.list(catalog_name=catalog, schema_name=schema, max_results=0, omit_columns=True)
    except TypeError:
        try:
            tables = client.tables.list(catalog_name=catalog, schema_name=schema)
        except Exception:
            return examined, examined >= _MAX_EXAMINED
    except Exception:
        return examined, examined >= _MAX_EXAMINED
    seen = 0
    try:
        for table in tables:
            examined += 1
            seen += 1
            full_name = getattr(table, "full_name", None) or f"{catalog}.{schema}.{getattr(table, 'name', '')}"
            if match_rank(query, full_name):
                objects.append(
                    {
                        "kind": "table",
                        "name": full_name,
                        "table_type": str(getattr(table, "table_type", "") or ""),
                    }
                )
            if seen >= _MAX_PER_SCHEMA or examined >= _MAX_EXAMINED:
                return examined, examined >= _MAX_EXAMINED
    except Exception:
        return examined, examined >= _MAX_EXAMINED
    return examined, False


def _take_files(
    client,
    catalog: str,
    schema: str,
    query: str,
    objects: list[dict],
    examined: int,
) -> tuple[int, bool]:
    try:
        volumes = client.volumes.list(catalog_name=catalog, schema_name=schema, max_results=0)
    except TypeError:
        try:
            volumes = client.volumes.list(catalog_name=catalog, schema_name=schema)
        except Exception:
            return examined, examined >= _MAX_EXAMINED
    except Exception:
        return examined, examined >= _MAX_EXAMINED
    seen = 0
    try:
        for volume in volumes:
            if seen >= _MAX_VOLUMES:
                break
            seen += 1
            volume_name = getattr(volume, "name", None)
            if not volume_name:
                continue
            root = f"/Volumes/{catalog}/{schema}/{volume_name}"
            examined, stopped = _walk_files(client, root, query, objects, examined, depth=0)
            if stopped:
                return examined, True
    except Exception:
        return examined, examined >= _MAX_EXAMINED
    return examined, False


def _walk_files(
    client,
    directory: str,
    query: str,
    objects: list[dict],
    examined: int,
    *,
    depth: int,
) -> tuple[int, bool]:
    if ".." in directory.split("/"):
        return examined, False
    try:
        entries = client.files.list_directory_contents(directory)
    except Exception:
        return examined, False
    try:
        for entry in entries:
            examined += 1
            name = getattr(entry, "name", None) or ""
            path = getattr(entry, "path", None) or f"{directory.rstrip('/')}/{name}"
            if ".." in path.split("/"):
                continue
            is_dir = bool(getattr(entry, "is_directory", False))
            if is_dir and depth < _MAX_FILE_DEPTH and (match_rank(query, name) or depth == 0):
                examined, stopped = _walk_files(client, path.rstrip("/"), query, objects, examined, depth=depth + 1)
                if stopped:
                    return examined, True
            elif not is_dir and _DATA_FILE.search(name):
                objects.append({"kind": "volume_file", "name": path, "table_type": "file"})
            if examined >= _MAX_EXAMINED:
                return examined, True
    except Exception:
        return examined, examined >= _MAX_EXAMINED
    return examined, False
