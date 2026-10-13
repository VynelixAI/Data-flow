import json

from app.databricks.identifiers import parse_uc_location, quote_fqn, safe_upload_stem, sql_relation, volume_uri
from app.databricks.notebook_executor import render_pipeline_notebook, render_validation_notebook
from app.databricks.validation import assemble_report, build_validation_plan, run_validation
from app.errors import DataflowError
from app.schemas import ColumnSpec, PipelineSpec, Repair, SchemaDrift, ValidationRequest


def _columns() -> list[ColumnSpec]:
    return [
        ColumnSpec(name="order_id", data_type="bigint"),
        ColumnSpec(name="line_id", data_type="int"),
        ColumnSpec(name="status", data_type="string"),
        ColumnSpec(name="amount", data_type="double"),
        ColumnSpec(name="email", data_type="string"),
        ColumnSpec(name="payload", data_type="string"),
        ColumnSpec(name="region", data_type="string"),
        ColumnSpec(name="domain_published_ts", data_type="timestamp"),
        ColumnSpec(name="is_deleted", data_type="boolean"),
    ]


def _request(**overrides) -> ValidationRequest:
    payload = dict(
        source_table="src_cat.src_sch.src_table",
        target_table="tgt_cat.tgt_sch.tgt_table",
        primary_keys=["order_id", "line_id"],
        compare_columns=["status", "amount"],
        soft_delete_column="is_deleted",
        timestamp_alignment="common_max",
        float_tolerance=0.01,
        json_columns=["payload"],
        formats={"email": "email"},
        aggregate_dimensions=["region"],
        aggregate_measures=["amount"],
        not_null_columns=["status"],
        source_columns=_columns(),
        target_columns=_columns(),
        execute=False,
    )
    payload.update(overrides)
    return ValidationRequest(**payload)


def _sql(name: str, steps) -> str:
    return next(step.sql for step in steps if step.name == name)


def test_validation_sql_covers_the_reconciliation_contract():
    steps, drift, warnings, _pairs = build_validation_plan(_request(), _columns(), _columns())
    metrics = _sql("metrics", steps)
    assert "FULL OUTER JOIN" in metrics
    assert "domain_published_ts" in metrics
    assert "LEAST(" in metrics
    assert "s.`order_id` = t.`order_id` AND s.`line_id` = t.`line_id`" in metrics
    assert "ABS(CAST(s.`amount` AS DOUBLE) - CAST(t.`amount` AS DOUBLE)) > 0.01" in metrics
    assert "s.`_dataflow_present` IS NOT NULL AND t.`_dataflow_present` IS NOT NULL" in metrics
    assert "is_deleted" in metrics
    assert "ROW_NUMBER()" in metrics
    samples = _sql("mismatch_samples", steps)
    assert "MISSING_IN_TARGET" in samples
    assert "MISSING_IN_SOURCE" in samples
    assert "VALUE_MISMATCH" in samples
    quality = _sql("quality_checks", steps)
    assert "try_parse_json" in quality
    assert "RLIKE" in quality
    aggregates = _sql("aggregates", steps)
    assert "SUM(CAST(`amount` AS DOUBLE))" in aggregates
    assert "GROUP BY" in aggregates
    assert drift.type_mismatches == []
    assert not any("No mapped" in warning for warning in warnings)


def test_datatype_mismatch_is_recorded_and_checked():
    target = _columns()
    target[3] = ColumnSpec(name="amount", data_type="string")
    steps, drift, _warnings, _pairs = build_validation_plan(_request(target_columns=target), _columns(), target)
    assert any(item["column"] == "amount" and item["severity"] == "error" for item in drift.type_mismatches)
    assert "try_cast" in _sql("quality_checks", steps)
    assert "<=>" in _sql("metrics", steps)


def test_rejects_injected_identifiers():
    for table in ("main.sch.orders;drop", "main.sch.orders)", "orders"):
        try:
            run_validation(_request(source_table=table, source_columns=None, target_columns=None))
        except DataflowError as exc:
            assert exc.code in {"invalid_table_name", "invalid_identifier"}
        else:
            raise AssertionError(f"{table} should have been rejected")


def test_report_status_pass_fail_and_late_data():
    request = _request()
    base = {
        "source_count": "10",
        "target_count": "10",
        "missing_in_target": "0",
        "missing_in_source": "0",
        "value_mismatch": "0",
        "duplicate_keys_source": "0",
        "duplicate_keys_target": "0",
        "duplicate_rows_source": "0",
        "duplicate_rows_target": "0",
        "null_pk_source": "0",
        "null_pk_target": "0",
        "source_used_ts": "2026-04-01 00:00:00",
        "target_used_ts": "2026-04-01 00:00:00",
        "source_max_ts": "2026-04-01 00:00:00",
        "target_max_ts": "2026-04-01 00:00:00",
    }
    passed = assemble_report(
        request,
        results={"metrics": [base], "mismatch_samples": []},
        step_errors={},
        drift=SchemaDrift(),
        warnings=[],
        dqx_checks=[],
        duplicate_keys_collapsed=True,
    )
    assert passed.status == "PASS"
    assert passed.mismatch_count == 0

    failed_rows = dict(base)
    failed_rows["target_count"] = "8"
    failed_rows["missing_in_target"] = "2"
    failed = assemble_report(
        request,
        results={
            "metrics": [failed_rows],
            "mismatch_samples": [
                {
                    "mismatch_type": "MISSING_IN_TARGET",
                    "primary_key_json": '{"order_id": "9", "line_id": "1"}',
                    "column_diffs_json": "[]",
                }
            ],
        },
        step_errors={},
        drift=SchemaDrift(),
        warnings=[],
        dqx_checks=[],
        duplicate_keys_collapsed=True,
    )
    assert failed.status == "FAIL"
    assert failed.mismatch_count == 2
    assert failed.quarantine_preview[0].primary_key["order_id"] == "9"
    assert failed.source_domain_published_ts == "2026-04-01 00:00:00"

    skewed = dict(base)
    skewed["target_used_ts"] = "2026-04-02 00:00:00"
    warning = assemble_report(
        request,
        results={"metrics": [skewed]},
        step_errors={},
        drift=SchemaDrift(),
        warnings=[],
        dqx_checks=[],
        duplicate_keys_collapsed=False,
    )
    assert warning.status == "PASS"
    assert any(item.startswith("LATE_ARRIVING") for item in warning.warnings)

    strict = _request(fail_on_timestamp_skew=True)
    skewed_fail = assemble_report(
        strict,
        results={"metrics": [skewed]},
        step_errors={},
        drift=SchemaDrift(),
        warnings=[],
        dqx_checks=[],
        duplicate_keys_collapsed=False,
    )
    assert skewed_fail.status == "FAIL"

    broken = assemble_report(
        request,
        results={},
        step_errors={"metrics": "warehouse exploded"},
        drift=SchemaDrift(),
        warnings=[],
        dqx_checks=[],
        duplicate_keys_collapsed=False,
    )
    assert broken.status == "ERROR"


def test_pipeline_notebook_is_parameterized_and_valid_python():
    spec = PipelineSpec(
        source_table="main.bronze.orders",
        target_table="main.silver.orders",
        primary_keys=["order_id"],
        json_columns=["payload"],
        repairs=[Repair(column="payload", action="parse_json", json_schema_ddl="STRUCT<sku: STRING>", flatten_json=True)],
        formats={"email": "email"},
        soft_delete_column="is_deleted",
    )
    code = render_pipeline_notebook(spec)
    compile(code, "<pipeline>", "exec")
    assert 'dbutils.widgets.text("source_table"' in code
    assert "DQEngine" in code
    assert "MERGE INTO" in code
    assert "from_json" in code
    assert "main.bronze.orders" in code
    assert "%sh" not in code
    assert "confirmation_required" in code
    assert code.index("confirmation_required") < code.index("saveAsTable")
    assert '"confirm_create_destination"' in code


def test_catalog_search_ranks_tables_and_files():
    from app.databricks.search import collect_hits, match_rank
    from app.errors import DataflowError
    from app.databricks.search import search_catalog

    assert match_rank("orders", "main.bronze.orders") == 100
    assert match_rank("orders", "/Volumes/main/dataflow/uploads/orders.csv") == 100
    assert match_rank("store inventory", "main.silver.store_inventory") == 40
    assert match_rank("zz", "main.bronze.orders") == 0
    hits = collect_hits(
        "orders",
        [
            {"kind": "table", "name": "main.bronze.order_lines"},
            {"kind": "volume_file", "name": "/Volumes/main/raw/files/orders.csv"},
            {"kind": "table", "name": "main.bronze.customers"},
        ],
    )
    assert hits[0]["name"].endswith("orders.csv")
    assert all("customers" not in hit["name"] for hit in hits)
    try:
        search_catalog("a")
    except DataflowError as exc:
        assert exc.code == "query_too_short"
    else:
        raise AssertionError("short query was accepted")


def test_missing_destination_creates_an_empty_table_sql():
    from app.databricks.destination import render_create_notebook, table_creation_sql

    sql = table_creation_sql("main.bronze.orders", "main.silver.orders_new")
    assert "CREATE TABLE IF NOT EXISTS `main`.`silver`.`orders_new`" in sql
    assert "WHERE 1 = 0" in sql
    assert "`main`.`bronze`.`orders`" in sql
    code = render_create_notebook("/Volumes/main/dataflow/uploads/orders.csv", "main.silver.orders_new")
    compile(code, "<create>", "exec")
    assert "saveAsTable" in code
    assert "limit(0)" in code
    file_code = render_create_notebook("main.bronze.orders", "/Volumes/main/dataflow/uploads/orders_new.csv")
    compile(file_code, "<create-file>", "exec")
    assert "to_csv" in file_code


def test_volume_lookup_uses_the_files_api(monkeypatch):
    from app.databricks.client import describe_uc_object

    class Files:
        def get_metadata(self, path):
            if path.endswith("missing.csv"):
                raise RuntimeError("RESOURCE_DOES_NOT_EXIST")
            return object()

        def get_directory_metadata(self, path):
            raise RuntimeError("RESOURCE_DOES_NOT_EXIST")

    class Client:
        files = Files()

    monkeypatch.setattr("app.databricks.client.get_workspace_client", lambda: Client())
    present = describe_uc_object("/Volumes/workspace/default/customervolume/customer_orders_raw.csv")
    missing = describe_uc_object("/Volumes/workspace/default/customervolume/missing.csv")
    assert present["exists"] is True
    assert present["checked"] is True
    assert missing["exists"] is False


def test_missing_volume_file_is_a_header_upload(monkeypatch):
    from app.databricks.destination import create_missing_destination

    uploaded: dict = {}

    class Column:
        def __init__(self, name):
            self.name = name

    class Table:
        columns = [Column("order_id"), Column("store_id")]

    class Tables:
        def get(self, full_name):
            return Table()

    class Files:
        def upload(self, path, contents, overwrite=False):
            uploaded["path"] = path
            uploaded["body"] = contents.read()
            uploaded["overwrite"] = overwrite

    class Client:
        files = Files()
        tables = Tables()

    seen = {"n": 0}

    def describe(name):
        seen["n"] += 1
        return {
            "checked": True,
            "exists": seen["n"] > 1,
            "name": name,
            "kind": "volume_file",
        }

    monkeypatch.setattr("app.databricks.destination.describe_uc_object", describe)
    monkeypatch.setattr("app.databricks.destination.get_workspace_client", lambda: Client())
    monkeypatch.setattr("app.databricks.destination._ensure_schema", lambda catalog, schema: None)
    monkeypatch.setattr("app.databricks.file_upload._ensure_volume", lambda catalog, schema, volume: None)

    result = create_missing_destination(
        "workspace.default.customer_orders_raw",
        "/Volumes/workspace/default/customervolume/customer_orders_new.csv",
    )
    assert result["created"] is True
    assert result["method"] == "files"
    assert uploaded["overwrite"] is False
    assert b"order_id" in uploaded["body"]
    assert b"store_id" in uploaded["body"]


def test_volume_files_are_unity_catalog_relations():
    source = "/Volumes/main/dataflow/uploads/orders.csv"
    target = "/Volumes/main/dataflow/uploads/orders_out.xlsx"
    assert sql_relation(source).startswith("read_files('/Volumes/main/dataflow/uploads/orders.csv'")
    assert "format => 'excel'" in sql_relation(target)
    steps, _drift, _warnings, _pairs = build_validation_plan(
        _request(source_table=source, target_table=target),
        _columns(),
        _columns(),
    )
    metrics = _sql("metrics", steps)
    assert "read_files('/Volumes/main/dataflow/uploads/orders.csv'" in metrics
    assert "read_files('/Volumes/main/dataflow/uploads/orders_out.xlsx'" in metrics
    try:
        parse_uc_location("/Volumes/main/dataflow/uploads/../secret.csv")
    except DataflowError:
        pass
    else:
        raise AssertionError("path traversal was accepted")


def test_agent_skills_discover_then_load():
    from app.agent.skills import discovery_text, list_skills, load_skill, validate_check_metadata

    names = {item["name"] for item in list_skills()}
    assert {"dqx-define-checks", "dqx-apply-checks", "dqx-end-to-end", "dqx-profile-and-generate", "dqx-storage", "dataflow-quality"} <= names
    catalog = discovery_text()
    assert "dqx-apply-checks:" in catalog
    assert "# DQX — Apply quality checks" not in catalog
    loaded = load_skill("dqx-apply-checks")
    assert "apply_checks_by_metadata" in loaded["instructions"]
    assert load_skill("dataflow-quality")["name"] == "dataflow-quality"
    validate_check_metadata(
        [{"name": "order_id_not_null", "criticality": "error", "check": {"function": "is_not_null", "arguments": {"column": "order_id"}}}]
    )
    try:
        validate_check_metadata([{"name": "broken", "criticality": "warning", "check": {"function": "is_not_null", "arguments": {}}}])
    except ValueError as exc:
        assert "error or warn" in str(exc)
    else:
        raise AssertionError("warning criticality should be rejected")


def test_memory_learns_slowly_and_skips_a_corrected_column(tmp_path):
    from app.agent.memory import apply_lessons, observe_episode, recall, remember, use_memory_path
    from app.databricks.dqx_utils import infer_quality_checks

    use_memory_path(tmp_path / "memory.json")
    columns = ["order_id", "email", "amount", "status"]
    first = remember(kind="skip_column", column="email", columns=columns)
    assert first["weight"] == 1
    second = remember(kind="skip_column", column="email", columns=columns)
    assert second["weight"] == 2
    checks = infer_quality_checks(columns)
    assert any(check["name"] == "email_valid_format" for check in checks)
    filtered = apply_lessons(checks, recall(columns))
    assert all("email" not in check["name"] for check in filtered)
    observe_episode(
        source="workspace.default.orders",
        destination="/Volumes/workspace/default/files/orders.csv",
        columns=columns,
        primary_keys=["order_id"],
        write_mode="merge",
        failing_checks=["amount_is_numeric"],
    )
    assert not any(lesson["kind"] == "primary_keys" for lesson in recall(columns))
    observe_episode(
        source="workspace.default.orders",
        destination="/Volumes/workspace/default/files/orders.csv",
        columns=columns,
        primary_keys=["order_id"],
        write_mode="merge",
    )
    learned_keys = [lesson for lesson in recall(columns) if lesson["kind"] == "primary_keys"]
    assert learned_keys
    assert learned_keys[0]["detail"] == "order_id"
    assert "amount=1007" not in (tmp_path / "memory.json").read_text()
    use_memory_path(None)


def test_dqx_rules_follow_the_source_columns():
    from app.databricks.dqx_utils import infer_quality_checks

    orders = ["order_id", "customer_id", "order_date", "amount", "status", "email", "shipping_region", "extra_info"]
    order_checks = {check["name"]: check for check in infer_quality_checks(orders)}
    assert order_checks["order_id_not_null"]["criticality"] == "error"
    assert order_checks["email_not_null"]["criticality"] == "warn"
    date_args = order_checks["order_date_is_valid_date"]["check"]["arguments"]
    assert "negate" not in date_args
    assert "yyyy-MM-dd" in date_args["expression"]
    assert "dd/MM/yyyy" in date_args["expression"]
    assert "MM/dd/yyyy" in date_args["expression"]
    assert date_args["expression"].startswith("`order_date` IS NULL OR ")
    assert "DECIMAL(18,2)) > 0" in order_checks["amount_greater_than_zero"]["check"]["arguments"]["expression"]
    assert "user@domain" in order_checks["email_valid_format"]["check"]["arguments"]["msg"]
    assert "try_parse_json" in order_checks["extra_info_valid_json"]["check"]["arguments"]["expression"]
    assert "status_not_null" in order_checks
    assert "shipping_region_not_null" not in order_checks
    typed = infer_quality_checks(["amount"], types={"amount": "double"})
    assert typed[0]["name"] == "amount_greater_than_zero"
    assert "RLIKE" not in typed[0]["check"]["arguments"]["expression"]
    cased = {
        check["name"]: check
        for check in infer_quality_checks(["status"], samples={"status": ["Shipped", "shipped", "Pending"]})
    }
    assert "shipped" in cased["status_case_inconsistent"]["check"]["arguments"]["expression"]
    assert cased["status_case_inconsistent"]["criticality"] == "warn"

    inventory = ["store_id", "sku", "quantity", "updated_at"]
    inventory_checks = {check["name"] for check in infer_quality_checks(inventory)}
    assert "store_id_not_null" in inventory_checks
    assert "quantity_is_numeric" in inventory_checks
    assert "updated_at_is_valid_date" in inventory_checks
    assert "email_valid_format" not in inventory_checks
    assert "order_date_is_valid_date" not in inventory_checks

    spec = PipelineSpec(
        source_table="workspace.default.customer_orders_raw",
        target_table="/Volumes/workspace/default/customervolume/customer_orders_raw.csv",
        primary_keys=["order_id"],
    )
    code = render_pipeline_notebook(spec, columns=orders)
    compile(code, "<orders>", "exec")
    assert "_checks_from_frame" in code
    assert "_apply_learned" in code
    assert "dataflow-quality" in code
    assert "apply_checks_by_metadata" in code
    assert "dqx_summary" in code
    assert "source_profile" in code
    assert "DQX_SPEC = json.loads(" in code
    assert "LEARNED_RULES = json.loads(" in code
    spec_line = next(line for line in code.splitlines() if line.startswith("DQX_SPEC = json.loads("))
    loaded: dict = {}
    exec("import json\n" + spec_line, loaded)
    date_rule = next(item for item in loaded["DQX_SPEC"] if item["name"] == "order_date_is_valid_date")
    assert date_rule["check"]["function"] == "sql_expression"
    assert "negate" not in date_rule["check"]["arguments"]
    assert "yyyy-MM-dd" in date_rule["check"]["arguments"]["expression"]
    assert all("negate" not in item["check"]["arguments"] for item in loaded["DQX_SPEC"])
    assert '"negate": false' not in code
    assert '"negate": False' not in code
    assert "Delivered" not in code


def test_chat_paths_replace_example_tables():
    from app.agent.tools import bind_user_locations
    from app.databricks.identifiers import locations_in_text

    text = (
        "source table workspace.default.customer_orders_raw, destination "
        "/Volumes/workspace/default/customervolume/customer_orders_raw.csv"
    )
    assert locations_in_text(text) == [
        "workspace.default.customer_orders_raw",
        "/Volumes/workspace/default/customervolume/customer_orders_raw.csv",
    ]
    bound = bind_user_locations(
        "validate_source_to_target",
        {
            "source_table": "main.bronze.orders",
            "target_table": "main.silver.orders",
            "compare_columns": [],
        },
        [{"role": "user", "content": text}, {"role": "user", "content": "yes"}],
    )
    assert bound["source_table"] == "workspace.default.customer_orders_raw"
    assert bound["target_table"] == "/Volumes/workspace/default/customervolume/customer_orders_raw.csv"
    assert bound["compare_columns"] is None
    profiled = bind_user_locations(
        "profile_table",
        {"table": "main.silver.orders", "primary_keys": ["order_id"]},
        [{"role": "user", "content": text}],
    )
    assert profiled["table"] == "workspace.default.customer_orders_raw"


def test_review_rejects_a_copy_onto_itself():
    from app.agent.review import review_copy
    from app.agent.session_memory import close_thread, get_run, open_run
    from app.schemas import PipelineSpec

    spec = PipelineSpec(
        source_table="main.bronze.orders",
        target_table="main.bronze.orders",
        primary_keys=["order_id"],
    )
    review = review_copy(spec, _columns(), _columns(), [])
    assert review["ok"] is False
    assert any("same object" in blocker for blocker in review["blockers"])
    record = open_run(notebook="print(1)", notes=["temporary"])
    assert get_run(record["review_id"])["notes"] == ["temporary"]
    close_thread("")
    assert get_run(record["review_id"]) is None


def test_notebook_code_is_not_a_catalog():
    from app.agent.tools import bind_user_locations, execute_tool
    from app.databricks.identifiers import coerce_location, locations_in_text, parse_uc_location
    from app.errors import DataflowError

    text = (
        'dbutils.widgets.text("source_table", "/Volumes/workspace/default/customervolume/customer_orders_raw.csv")\n'
        'dbutils.widgets.text("target_table", "/Volumes/workspace/default/customervolume/customer_orders_raw3_validated.csv")'
    )
    assert locations_in_text(text) == [
        "/Volumes/workspace/default/customervolume/customer_orders_raw.csv",
        "/Volumes/workspace/default/customervolume/customer_orders_raw3_validated.csv",
    ]
    bound = bind_user_locations(
        "create_destination",
        {"source_table": "dbutils.widgets.text", "target_table": "dbutils.widgets.text", "confirm": True},
        [{"role": "user", "content": text}],
    )
    assert bound["source_table"].startswith("/Volumes/workspace/default/customervolume/")
    assert bound["target_table"].endswith("customer_orders_raw3_validated.csv")
    widget_line = (
        'dbutils.widgets.text("target_table", "/Volumes/workspace/default/customervolume/customer_orders_raw3_validated.csv")'
    )
    assert coerce_location(widget_line).endswith("customer_orders_raw3_validated.csv")
    try:
        parse_uc_location("dbutils.widgets.text")
    except DataflowError as exc:
        assert exc.code == "invalid_table_name"
    else:
        raise AssertionError("dbutils.widgets.text was accepted as a table")
    refused = execute_tool(
        "inspect_uc_object",
        {"name": "dbutils.widgets.text"},
    )
    assert refused["result"]["code"] == "invalid_path"
    from app.agent.graph import _confirmed_locations

    source, destination = _confirmed_locations(
        "dbutils.widgets.text",
        "dbutils.widgets.text",
        [{"role": "user", "content": text}, {"role": "user", "content": "yes"}],
    )
    assert source.endswith("customer_orders_raw.csv")
    assert destination.endswith("customer_orders_raw3_validated.csv")


def test_empty_compare_list_checks_shared_columns():
    steps, _drift, _warnings, pairs = build_validation_plan(
        _request(compare_columns=[]),
        _columns(),
        _columns(),
    )
    compared = {pair[0].name for pair in pairs}
    assert "status" in compared
    assert "amount" in compared
    assert "1 = 0" not in _sql("metrics", steps)


def test_validation_compares_full_tables_without_a_snapshot_column():
    columns = [column for column in _columns() if column.name != "domain_published_ts"]
    steps, _drift, warnings, _pairs = build_validation_plan(_request(), columns, columns)
    metrics = _sql("metrics", steps)
    assert "domain_published_ts" not in metrics
    assert "1 = 1" in metrics
    assert any("full source and destination" in warning for warning in warnings)


def test_generate_copies_rows_and_returns_the_validation_report(monkeypatch, tmp_path):
    from app.agent.memory import use_memory_path
    from app.agent.tools import execute_tool
    from app.schemas import RunStatus

    use_memory_path(tmp_path / "memory.json")

    captured: dict = {}

    def lookup(name: str) -> dict:
        return {"name": name, "kind": "table", "exists": True, "checked": True}

    def fake_execute(code: str, **kwargs):
        captured["code"] = code
        captured["wait"] = kwargs.get("wait")
        captured["confirm"] = (kwargs.get("base_parameters") or {}).get("confirm_create_destination")
        payload = json.dumps(
            {
                "copy": {"valid_rows": 2, "quarantine_rows": 0, "target": "main.silver.orders"},
                "results": {
                    "metrics": [
                        {
                            "source_count": 2,
                            "target_count": 2,
                            "missing_in_target": 0,
                            "missing_in_source": 0,
                            "value_mismatch": 0,
                            "duplicate_keys_source": 0,
                            "duplicate_keys_target": 0,
                            "duplicate_rows_source": 0,
                            "duplicate_rows_target": 0,
                            "null_pk_source": 0,
                            "null_pk_target": 0,
                        }
                    ]
                },
                "errors": {},
            }
        )
        return RunStatus(
            run_id=7,
            life_cycle_state="TERMINATED",
            result_state="SUCCESS",
            finished=True,
            output=payload,
        )

    monkeypatch.setattr("app.agent.tools.describe_uc_object", lookup)
    monkeypatch.setattr("app.agent.tools.execute_notebook", fake_execute)
    monkeypatch.setattr(
        "app.agent.tools._columns_for_copy",
        lambda source, target: (_columns(), _columns(), []),
    )
    result = execute_tool(
        "generate_pipeline",
        {
            "source_table": "main.bronze.orders",
            "target_table": "main.silver.orders",
            "primary_keys": ["order_id", "line_id"],
            "compare_columns": ["status", "amount"],
        },
    )
    assert "code" not in captured
    assert result["result"]["confirmation_required"] is True
    assert result["result"]["kind"] == "run"
    assert result["result"]["copied"] is False
    assert any("shared column" in note or "status" in note for note in result["result"]["notes"])
    assert result["artifacts"][0]["kind"] == "code"
    assert result["artifacts"][1]["confirmation_kind"] == "run"
    from app.agent.session_memory import get_run
    from app.agent.tools import run_prepared_copy

    review_id = result["result"]["review_id"]
    assert get_run(review_id) is not None
    finished = run_prepared_copy(review_id, "keep the primary key")
    assert captured["wait"] is True
    assert captured["confirm"] == "true"
    assert captured["code"].startswith("# Operator comment for this run:")
    assert "spark.sql" in captured["code"]
    assert "main.bronze.orders" in captured["code"]
    kinds = [artifact["kind"] for artifact in finished["artifacts"]]
    assert "execution" in kinds
    assert "validation" in kinds
    report = next(artifact["report"] for artifact in finished["artifacts"] if artifact["kind"] == "validation")
    assert report["status"] == "PASS"
    assert report["source_count"] == 2
    assert finished["result"]["copied"] is True
    assert finished["result"]["valid_rows"] == 2
    assert get_run(review_id) is None
    use_memory_path(None)


def test_missing_destination_asks_before_creating(monkeypatch):
    from app.agent.tools import execute_tool
    from app.schemas import RunStatus

    monkeypatch.setattr(
        "app.agent.tools._columns_for_copy",
        lambda source, target: (_columns(), _columns(), []),
    )
    monkeypatch.setattr(
        "app.agent.tools.execute_notebook",
        lambda code, **kwargs: RunStatus(run_id=1, life_cycle_state="TERMINATED", result_state="SUCCESS", finished=True, output="{}"),
    )

    def lookup(name: str) -> dict:
        kind = "volume_file" if name.startswith("/Volumes/") else "table"
        return {"name": name, "kind": kind, "exists": name == "main.bronze.orders", "checked": True}

    monkeypatch.setattr("app.agent.tools.describe_uc_object", lookup)
    result = execute_tool(
        "generate_pipeline",
        {
            "source_table": "main.bronze.orders",
            "target_table": "main.silver.orders_new",
            "primary_keys": ["order_id"],
        },
    )
    assert result["result"]["confirmation_required"] is True
    assert result["artifacts"][0]["kind"] == "confirmation"
    assert result["artifacts"][0]["destination"] == "main.silver.orders_new"

    created = execute_tool(
        "generate_pipeline",
        {
            "source_table": "main.bronze.orders",
            "target_table": "/Volumes/main/dataflow/uploads/out.csv",
            "primary_keys": ["order_id"],
            "confirm_create_destination": True,
        },
    )
    assert "notebook_preview" in created["result"]
    assert created["artifacts"][0]["kind"] == "code"
    assert "confirmation_required" in created["artifacts"][0]["content"]

    blocked = execute_tool(
        "generate_pipeline",
        {
            "source_table": "main.bronze.missing",
            "target_table": "main.silver.orders",
            "primary_keys": ["order_id"],
            "confirm_create_destination": True,
        },
    )
    assert blocked["result"]["code"] == "source_missing"
    assert blocked["artifacts"] == []


def test_validation_notebook_embeds_sql():
    request = _request()
    steps, _drift, _warnings, _pairs = build_validation_plan(request, _columns(), _columns())
    code = render_validation_notebook(request, steps)
    compile(code, "<validation>", "exec")
    assert "spark.sql" in code
    assert "FULL OUTER JOIN" in code


def test_upload_names_and_excel_normalization():
    assert safe_upload_stem("My Orders (1).xlsx") == "my_orders_1"
    assert volume_uri("main", "dataflow", "uploads", "uploads", "20261007T120000Z", "orders.csv").endswith(
        "/orders.csv"
    )
    try:
        volume_uri("main", "dataflow", "uploads", "..", "orders.csv")
    except DataflowError:
        pass
    else:
        raise AssertionError("parent path segment was accepted")
    try:
        quote_fqn("main.sales.orders;drop table")
    except DataflowError:
        pass
    else:
        raise AssertionError("fqn injection was accepted")

    from openpyxl import Workbook

    from app.databricks.file_upload import _to_csv_bytes

    book = Workbook()
    sheet = book.active
    sheet.title = "Orders"
    sheet.append(["Order ID", "Order ID", "Payload"])
    sheet.append([1, 2, '{"sku": "A"}'])
    import io

    buffer = io.BytesIO()
    book.save(buffer)
    csv_bytes, chosen, has_timestamp = _to_csv_bytes(buffer.getvalue(), "orders.xlsx", None)
    text = csv_bytes.decode()
    assert chosen == "Orders"
    header = text.splitlines()[0]
    assert header == "order_id,order_id_1,payload"
    assert has_timestamp is False
    assert '{"sku": "A"}' in text or '{""sku"": ""A""}' in text


def test_warehouse_id_accepts_the_sql_http_path(monkeypatch):
    from app.databricks.client import warehouse_id

    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "/sql/1.0/warehouses/7f0a512e4f1a41ef")
    assert warehouse_id() == "7f0a512e4f1a41ef"
    monkeypatch.setenv("DATABRICKS_WAREHOUSE_ID", "7f0a512e4f1a41ef")
    assert warehouse_id() == "7f0a512e4f1a41ef"


def test_list_tables_reports_the_databricks_error(monkeypatch):
    from app.databricks.client import list_tables

    class Tables:
        def list(self, catalog_name, schema_name, include_browse=None):
            raise RuntimeError("SCHEMA_DOES_NOT_EXIST")

    class Client:
        tables = Tables()

    monkeypatch.setattr("app.databricks.client.get_workspace_client", lambda: Client())
    try:
        list_tables("workspace", "default")
    except DataflowError as exc:
        assert "SCHEMA_DOES_NOT_EXIST" in exc.message
        assert "workspace.default" in exc.message
    else:
        raise AssertionError("list_tables should surface the Databricks error")


def test_upload_catalog_skips_missing_main(monkeypatch):
    from app.databricks.client import resolve_upload_catalog

    class Catalog:
        def __init__(self, name):
            self.name = name

    class Catalogs:
        def list(self):
            return [Catalog("system"), Catalog("samples"), Catalog("workspace")]

    class Client:
        catalogs = Catalogs()

    monkeypatch.setattr("app.databricks.client.get_workspace_client", lambda: Client())
    monkeypatch.delenv("DATABRICKS_CATALOG", raising=False)
    assert resolve_upload_catalog(None) == "workspace"
    monkeypatch.setenv("DATABRICKS_CATALOG", "main")
    assert resolve_upload_catalog(None) == "workspace"
    assert resolve_upload_catalog("workspace") == "workspace"


def test_dqx_samples_become_quarantine_rows():
    from app.databricks.validation import dqx_quarantine_rows

    rows = dqx_quarantine_rows(
        [
            {
                "name": "amount_numeric",
                "message": "amount must be numeric",
                "count": 1,
                "criticality": "error",
                "samples": ["order_id=1007"],
            }
        ]
    )
    assert rows[0].primary_key == {"order_id": "1007"}
    assert rows[0].column_diffs[0].column == "amount_numeric"
    assert rows[0].column_diffs[0].source_value == "amount must be numeric"


def test_validation_without_warehouse_runs_a_notebook(monkeypatch):
    monkeypatch.delenv("DATABRICKS_WAREHOUSE_ID", raising=False)
    monkeypatch.setattr(
        "app.databricks.client.env",
        lambda name, default=None: None if name == "DATABRICKS_WAREHOUSE_ID" else __import__("os").getenv(name, default),
    )

    def describe(name):
        return {"checked": True, "exists": True, "name": name}

    class Status:
        result_state = "SUCCESS"
        finished = True
        error = None
        state_message = None
        output = json.dumps(
            {
                "results": {
                    "metrics": [
                        {
                            "source_count": 10,
                            "target_count": 8,
                            "duplicate_keys_source": 1,
                            "null_pk_source": 2,
                        }
                    ]
                },
                "errors": {},
                "source_profile": {"table": "src_cat.src_sch.src_table", "row_count": 10},
            }
        )

    monkeypatch.setattr("app.databricks.client.describe_uc_object", describe)
    monkeypatch.setattr(
        "app.databricks.notebook_executor.execute_notebook",
        lambda code, **kwargs: Status(),
    )
    outcome = run_validation(_request(execute=True))
    assert outcome.report is not None
    assert outcome.report.source_count == 10
    assert outcome.report.target_count == 8
    assert outcome.report.duplicate_keys_source == 1
    assert outcome.report.null_pk_source == 2
    assert outcome.notebook
