"""FastAPI application: streaming chat, validation, uploads, and notebook runs."""

from __future__ import annotations

import json
from pathlib import Path

from dotenv import load_dotenv

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(_REPO_ROOT / ".env", override=False)
load_dotenv(_BACKEND_ROOT / ".env", override=False)

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app.agent.agent import stream_agent
from app.databricks.client import databricks_host, describe_uc_object, iter_health, list_tables
from app.databricks.search import search_catalog
from app.databricks.dqx_utils import build_dqx_checks
from app.databricks.file_upload import upload_and_create_table
from app.databricks.notebook_executor import (
    execute_notebook,
    get_run_status,
    pipeline_parameters,
    render_pipeline_notebook,
    render_validation_notebook,
)
from app.databricks.validation import run_validation
from app.errors import DataflowError
from app.schemas import ChatRequest, ExecuteNotebookRequest, PipelineSpec, ValidationRequest

app = FastAPI(title="DataFlow Agent", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:4173",
        "http://127.0.0.1:4173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(DataflowError)
async def handle_dataflow_error(_request, exc: DataflowError):
    return JSONResponse(status_code=exc.status, content={"error": exc.message, "code": exc.code})


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


@app.get("/api/health")
def health():
    body = {key: value for key, value in iter_health()}
    body["status"] = "ok"
    body["databricks_host"] = databricks_host()
    body["agent_runtime"] = "langgraph"
    return body


@app.get("/api/uc/inspect")
def inspect_uc(name: str):
    """Check a Unity Catalog table or volume file. Does not create it."""
    return describe_uc_object(name)


@app.get("/api/catalog/tables")
def catalog_tables(catalog: str, schema_name: str):
    return {"catalog": catalog, "schema": schema_name, "tables": list_tables(catalog, schema_name)}


@app.get("/api/catalog/search")
def catalog_search(q: str, catalog: str | None = None, schema_name: str | None = None):
    return search_catalog(q, catalog=catalog or None, schema_name=schema_name or None)


@app.post("/api/chat")
async def chat(body: ChatRequest):
    async def events():
        try:
            async for event in stream_agent(body):
                yield _sse(event)
        except DataflowError as exc:
            yield _sse({"type": "error", "message": exc.message, "code": exc.code})
            yield _sse({"type": "done"})
        except Exception as exc:  # pragma: no cover
            yield _sse({"type": "error", "message": str(exc), "code": "chat_failed"})
            yield _sse({"type": "done"})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/validate")
def validate(body: ValidationRequest):
    outcome = run_validation(body)
    if body.include_notebook:
        outcome.notebook = render_validation_notebook(body, outcome.sql_steps)
    return outcome


@app.post("/api/generate")
def generate(body: PipelineSpec):
    notebook = render_pipeline_notebook(body)
    checks = build_dqx_checks(
        primary_keys=body.primary_keys,
        not_null_columns=body.not_null_columns or body.primary_keys,
        formats=body.formats,
        json_columns=body.json_columns,
        compare_columns=body.compare_columns,
    )
    return {
        "notebook": notebook,
        "parameters": pipeline_parameters(body),
        "dqx_checks": checks,
    }


@app.post("/api/notebooks/execute")
def execute(body: ExecuteNotebookRequest):
    return execute_notebook(
        body.code,
        notebook_name=body.notebook_name,
        base_parameters=body.base_parameters,
        wait=body.wait,
        timeout_seconds=body.timeout_seconds,
    )


@app.get("/api/runs/{run_id}")
def run_status(run_id: int):
    return get_run_status(run_id)


@app.post("/api/upload")
async def upload(
    file: UploadFile = File(...),
    catalog: str | None = Form(default=None),
    schema_name: str | None = Form(default=None),
    volume: str | None = Form(default=None),
    table_name: str | None = Form(default=None),
    sheet: str | None = Form(default=None),
):
    content = await file.read()
    return upload_and_create_table(
        content,
        file.filename or "upload.csv",
        catalog=catalog,
        schema=schema_name,
        volume=volume,
        table_name=table_name,
        sheet=sheet,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
