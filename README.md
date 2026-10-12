# DataFlow Agent

DataFlow Agent generates parameterized Spark and DQX notebooks for Databricks, reconciles a source table with a target table, uploads CSV or Excel into Unity Catalog, and can run the notebook as a job. The UI is a streaming chat. The same actions are available as HTTP endpoints, so a person can validate or upload without waiting on the model.

Sources and destinations are expected to change. Generated notebooks read table names, keys, and the published-timestamp column from widgets. The logic does not have to be edited when a path changes.

## Architecture

```text
React (Vite)  --SSE / JSON-->  FastAPI
                                 |-- OpenAI tool-calling agent
                                 |-- Databricks SDK
                                      |-- Unity Catalog metadata
                                      |-- SQL warehouse (validation, profile, CREATE TABLE)
                                      |-- Workspace notebooks + Jobs API
```

Validation is computed in Spark SQL on a warehouse. The same SQL is embedded in a notebook so it can run on a job cluster. DQX checks are generated as metadata and applied inside the pipeline notebook, which splits valid rows from a quarantine table.

## What the validation checks

Both tables are read with fully qualified names. The comparison grain is one or more primary-key columns. Each side is limited to a single `domain_published_ts` (the column name is configurable):

- `latest_each` compares each table's own maximum timestamp.
- `common_max` compares the older of the two maxima, which avoids a false failure when the target publish is late.
- An explicit `source_published_ts` or `target_published_ts` pins that side.

Soft-deleted rows are removed when `soft_delete_column` is set (`true`, `1`, `yes`, `y`). Duplicate keys are counted, then collapsed with `ROW_NUMBER` so a full outer join cannot multiply mismatches. Null keys are counted separately and excluded from the grain.

The report includes:

| Field | Meaning |
| --- | --- |
| `status` | `PASS`, `FAIL`, or `ERROR` |
| `source_count`, `target_count` | Rows in the selected snapshot |
| `mismatch_count` | Missing in target + missing in source + value mismatches |
| `mismatched_keys` | Primary keys and per-column differences, capped |
| `source_domain_published_ts`, `target_domain_published_ts` | Timestamps actually used |
| `execution_ts` | UTC time the report was assembled |

Also checked: duplicate keys, null keys, nulls on required columns, format rules, JSON parse failures, schema drift, datatype families, and aggregates (`count`, `sum`, `min`, `max`) by business dimensions.

Nulls on both sides of a mapped column count as equal. Numbers compare with `float_tolerance`. Strings are trimmed. A timestamp skew is a warning unless `fail_on_timestamp_skew` is true. Columns that exist on only one side are drift warnings. A compared column whose type family differs (string versus double, for example) fails the report.

The core shape of the generated SQL is:

```sql
WITH source_latest AS (
  SELECT * FROM source_catalog.source_schema.source_table
  WHERE domain_published_ts = (
    SELECT MAX(domain_published_ts) FROM source_catalog.source_schema.source_table
  )
),
target_latest AS (
  SELECT * FROM target_catalog.target_schema.target_table
  WHERE domain_published_ts = (
    SELECT MAX(domain_published_ts) FROM target_catalog.target_schema.target_table
  )
)
SELECT ...
FROM source_latest s
FULL OUTER JOIN target_latest t
  ON s.primary_key = t.primary_key;
```

The implementation adds the grain dedupe, soft-delete filter, and column predicates described above. Identifiers are quoted only after they match `^[A-Za-z_][A-Za-z0-9_]*$`.

## Prerequisites

- Python 3.11+
- Node.js 20+
- A Databricks workspace on the host below, with a personal access token or service principal token
- A SQL warehouse id for profiling, validation, and table creation
- An OpenAI API key for the chat agent
- Permission to create schemas, volumes, tables, notebooks, and jobs

Serverless notebook execution is the default. Set `DATABRICKS_CLUSTER_ID` to pin an existing cluster instead.

## Configure

```bash
cp backend/.env.example backend/.env
```

| Variable | Purpose |
| --- | --- |
| `OPENAI_API_KEY` | Chat agent. Other endpoints work without it. |
| `OPENAI_MODEL` | Defaults to `gpt-4.1`. |
| `DATABRICKS_HOST` | `https://dbc-332aa8a0-08f7.cloud.databricks.com` |
| `DATABRICKS_TOKEN` | Workspace token. Never commit this. |
| `DATABRICKS_WAREHOUSE_ID` | SQL warehouse for queries and `CREATE TABLE`. |
| `DATABRICKS_USE_SERVERLESS` | `true` submits a serverless notebook task. |
| `DATABRICKS_CLUSTER_ID` | Optional all-purpose or job cluster. |
| `DATABRICKS_CATALOG` / `DATABRICKS_SCHEMA` / `DATABRICKS_VOLUME` | Upload destination. Defaults: `main.dataflow.uploads`. |
| `DATABRICKS_NOTEBOOK_DIR` | Workspace folder for generated notebooks. |
| `DATABRICKS_SQL_TIMEOUT` | Seconds to wait for a warehouse statement. |

The API reads `backend/.env` and a `.env` in the repo root. Existing shell variables win.

## Run locally

```bash
cd backend
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Open http://localhost:5173. The Vite dev server proxies `/api` to port 8000.

Header pills show whether the token, warehouse, and OpenAI key are present. They do not show the secret values.

## Using the product

1. Upload a CSV or Excel file from the composer. The file is written to a Unity Catalog volume and loaded with `read_files` into a Delta table. Excel headers are normalized to identifiers. `domain_published_ts` is added when the file does not already have that column. The fully qualified name appears as a chip and is sent to the agent on the next message.
2. Ask the chat to profile a table, repair columns, or generate a pipeline. Responses stream token by token. Tool steps show up above the answer. The notebook opens in the Code panel.
3. Ask for a source-to-target check, or use the Validation panel. Leave "Execute on the SQL warehouse" off to generate SQL and a notebook without a warehouse. Turn it on to run the reconciliation and fill the report.
4. Run the notebook from the Run panel, or ask the agent to execute it. The agent will not submit a job until you ask it to. The notebook is imported under `DATABRICKS_NOTEBOOK_DIR/generated` and submitted as a one-time job. Poll status with the run id.

Generated pipelines repair columns, apply DQX checks, append failures to a quarantine table or file, and merge valid rows on the primary key. `overwrite` and `append` are available write modes. Source and destination are Unity Catalog tables (`catalog.schema.table`) or CSV/Excel files in a volume (`/Volumes/catalog/schema/volume/file.csv`). If the destination is missing, the agent asks before creating it. The notebook exits with `confirmation_required` until `confirm_create_destination` is true.

Format rules understood by validation and DQX generation: `email`, `iso_date`, `iso_timestamp`, `uuid`, `integer`, `non_empty`, `json`, and `regex:<pattern>`.

## HTTP API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Configuration flags, no secrets |
| `POST` | `/api/chat` | Server-sent events: `token`, `tool_start`, `tool_end`, `artifact`, `error`, `done` |
| `POST` | `/api/validate` | Build or execute a reconciliation. `execute: false` returns SQL only |
| `POST` | `/api/generate` | Parameterized Spark + DQX notebook |
| `POST` | `/api/upload` | Multipart file. Optional form fields: `catalog`, `schema_name`, `volume`, `table_name`, `sheet` |
| `POST` | `/api/notebooks/execute` | Import a notebook and start a job |
| `GET` | `/api/runs/{run_id}` | Life cycle, result, and notebook output |

Example validation body:

```json
{
  "source_table": "main.bronze.orders",
  "target_table": "main.silver.orders",
  "primary_keys": ["order_id", "line_id"],
  "compare_columns": ["status", "amount"],
  "timestamp_column": "domain_published_ts",
  "timestamp_alignment": "latest_each",
  "float_tolerance": 0.000001,
  "null_equivalence": true,
  "soft_delete_column": "is_deleted",
  "json_columns": ["payload"],
  "formats": {"email": "email"},
  "aggregate_dimensions": ["region"],
  "aggregate_measures": ["amount"],
  "execute": true
}
```

Chat events look like `data: {"type":"token","content":"..."}`. Artifacts carry the notebook, the validation report, the quarantine sample, or the job status so the side panel can update while the answer is still streaming.

## Tests

```bash
cd backend
source .venv/bin/activate
pytest
```

The tests generate SQL and notebooks locally. They do not call Databricks or OpenAI.

## Docker

```bash
cp backend/.env.example backend/.env
docker compose up --build
```

The frontend container proxies `/api` to the backend service. Set `API_PROXY_TARGET` only if the backend is not reachable as `http://backend:8000`.

## Token scope and guards

Use a token that can read the source tables, create the destination objects, and submit jobs. The service never writes the token into notebooks or API responses.

Notebooks are rejected if they contain `%sh`, `%fs`, `dbutils.fs.rm`, or `DROP CATALOG` / `DROP SCHEMA` / `DROP DATABASE`. This is a guardrail, not a sandbox. Generated code runs with the permissions of the job identity. Review the Code panel before you press Run.

Uploads are limited to 50 MB. Table, catalog, schema, and volume names must be simple identifiers.

## Project layout

```text
backend/app/main.py                         FastAPI and SSE
backend/app/agent/agent.py                  Streaming tool loop
backend/app/agent/tools.py                  Databricks, DQX, validation, execute
backend/app/databricks/validation.py        Source-to-target SQL and report
backend/app/databricks/file_upload.py       Volume upload and Delta table
backend/app/databricks/notebook_executor.py Notebook render and job submit
frontend/src/components/Chat.tsx            Streaming chat
frontend/src/components/FileUpload.tsx      CSV and Excel upload
frontend/src/components/CodeViewer.tsx
frontend/src/components/ValidationReport.tsx
frontend/src/components/ExecutionStatus.tsx
```
# Data-flow
