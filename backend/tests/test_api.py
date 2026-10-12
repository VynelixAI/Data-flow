from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health():
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["agent_runtime"] == "langgraph"
    assert "openai_configured" in body
    assert isinstance(body["databricks_token_configured"], bool)
    assert "dapi" not in response.text


def test_inspect_reports_unchecked_without_creating():
    response = client.get("/api/uc/inspect", params={"name": "main.silver.orders"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "table"
    if body["checked"]:
        assert isinstance(body["exists"], bool)
    else:
        assert body["exists"] is None


def test_catalog_rejects_unsafe_names():
    response = client.get("/api/catalog/tables", params={"catalog": "main;drop", "schema_name": "sales"})
    assert response.status_code == 400


def test_validate_dry_run_returns_sql_and_notebook():
    response = client.post(
        "/api/validate",
        json={
            "source_table": "main.bronze.orders",
            "target_table": "main.silver.orders",
            "primary_keys": ["order_id", "line_id"],
            "compare_columns": ["status"],
            "execute": False,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["report"] is None
    joined = "\n".join(step["sql"] for step in body["sql_steps"])
    assert "FULL OUTER JOIN" in joined
    assert "domain_published_ts" in joined
    assert "s.`order_id` = t.`order_id` AND s.`line_id` = t.`line_id`" in joined
    assert "dbutils.notebook.exit" in body["notebook"]
    assert body["dqx_checks"]


def test_validate_rejects_injection():
    response = client.post(
        "/api/validate",
        json={
            "source_table": "main.bronze.orders; drop table orders",
            "target_table": "main.silver.orders",
            "primary_keys": ["order_id"],
            "execute": False,
        },
    )
    assert response.status_code == 400
    assert response.json()["code"] in {"invalid_table_name", "invalid_identifier"}


def test_generate_pipeline():
    response = client.post(
        "/api/generate",
        json={
            "source_table": "main.bronze.orders",
            "target_table": "main.silver.orders",
            "primary_keys": ["order_id"],
            "write_mode": "merge",
            "json_columns": ["payload"],
        },
    )
    assert response.status_code == 200
    notebook = response.json()["notebook"]
    compile(notebook, "<nb>", "exec")
    assert "widgets" in notebook
    assert response.json()["parameters"]["source_table"] == "main.bronze.orders"


def test_execute_rejects_shell_magic():
    response = client.post(
        "/api/notebooks/execute",
        json={"code": "# Databricks notebook source\n%sh ls\n", "wait": False},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "notebook_rejected"


def test_chat_streams_a_configuration_error_without_openai(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    response = client.post("/api/chat", json={"messages": [{"role": "user", "content": "Validate orders"}]})
    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    assert "OPENAI_API_KEY" in response.text
    assert "data: " in response.text
