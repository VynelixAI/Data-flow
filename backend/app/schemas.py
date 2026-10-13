"""Request and response models shared by the API, agent tools, and Databricks layer."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ColumnSpec(BaseModel):
    name: str
    data_type: str = "string"
    nullable: bool = True


class ChatMessage(BaseModel):
    role: Literal["user", "assistant", "system"]
    content: str


class UploadedTableContext(BaseModel):
    table: str
    volume_path: str
    columns: list[ColumnSpec] = Field(default_factory=list)
    row_count: int | None = None


class ChatContext(BaseModel):
    uploaded_tables: list[UploadedTableContext] = Field(default_factory=list)


class ResumeDecision(BaseModel):
    """Operator answer for a paused create or notebook-review step."""

    approved: bool
    destination: str = ""
    comment: str = ""
    action: Literal["approve", "decline", "revise"] = "approve"


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    context: ChatContext = Field(default_factory=ChatContext)
    thread_id: str | None = None
    resume: ResumeDecision | None = None

    @field_validator("messages")
    @classmethod
    def limit_history(cls, value: list[ChatMessage]) -> list[ChatMessage]:
        if not value:
            raise ValueError("messages must not be empty")
        if value[-1].role != "user":
            raise ValueError("the last message must be from the user")
        return value[-40:]


class ColumnMapping(BaseModel):
    source: str
    target: str | None = None
    transform: Literal[
        "identity",
        "trim",
        "lower",
        "upper",
        "cast",
        "from_json",
        "to_timestamp",
        "empty_to_null",
        "regex_replace",
    ] = "identity"
    cast_to: str | None = None
    timestamp_format: str | None = None
    pattern: str | None = None
    replacement: str | None = None
    json_schema_ddl: str | None = None
    flatten_json: bool = False


class Repair(BaseModel):
    column: str
    action: Literal[
        "trim",
        "empty_to_null",
        "cast",
        "coalesce",
        "parse_timestamp",
        "parse_json",
        "standardize_boolean",
        "negative_to_null",
    ]
    cast_to: str | None = None
    default: str | None = None
    timestamp_format: str | None = None
    json_schema_ddl: str | None = None
    flatten_json: bool = False


class PipelineSpec(BaseModel):
    source_table: str
    target_table: str
    primary_keys: list[str]
    compare_columns: list[str] | None = None
    column_mappings: list[ColumnMapping] = Field(default_factory=list)
    repairs: list[Repair] = Field(default_factory=list)
    json_columns: list[str] = Field(default_factory=list)
    timestamp_column: str = "domain_published_ts"
    soft_delete_column: str | None = None
    write_mode: Literal["merge", "overwrite", "append"] = "merge"
    not_null_columns: list[str] = Field(default_factory=list)
    formats: dict[str, str] = Field(default_factory=dict)
    float_tolerance: float = 1e-6
    quarantine_suffix: str = "_quarantine"
    confirm_create_destination: bool = False
    notes: str | None = None


class ValidationRequest(BaseModel):
    source_table: str
    target_table: str
    primary_keys: list[str] = Field(min_length=1)
    compare_columns: list[str] | None = None
    timestamp_column: str = "domain_published_ts"
    timestamp_alignment: Literal["latest_each", "common_max"] = "latest_each"
    source_published_ts: str | None = None
    target_published_ts: str | None = None
    float_tolerance: float = 1e-6
    null_equivalence: bool = True
    soft_delete_column: str | None = None
    exclude_columns: list[str] = Field(default_factory=list)
    not_null_columns: list[str] = Field(default_factory=list)
    json_columns: list[str] = Field(default_factory=list)
    formats: dict[str, str] = Field(default_factory=dict)
    aggregate_dimensions: list[str] = Field(default_factory=list)
    aggregate_measures: list[str] = Field(default_factory=list)
    max_mismatch_rows: int = 100
    fail_on_timestamp_skew: bool = False
    execute: bool = False
    include_notebook: bool = True
    source_columns: list[ColumnSpec] | None = None
    target_columns: list[ColumnSpec] | None = None

    @field_validator("max_mismatch_rows")
    @classmethod
    def cap_rows(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_mismatch_rows must be positive")
        return min(value, 1000)

    @field_validator("float_tolerance")
    @classmethod
    def tolerance_range(cls, value: float) -> float:
        if value < 0:
            raise ValueError("float_tolerance must be >= 0")
        return value


class ColumnDiff(BaseModel):
    column: str
    source_value: str | None = None
    target_value: str | None = None


class MismatchRow(BaseModel):
    primary_key: dict[str, str | None]
    mismatch_type: Literal["MISSING_IN_TARGET", "MISSING_IN_SOURCE", "VALUE_MISMATCH"]
    column_diffs: list[ColumnDiff] = Field(default_factory=list)


class DuplicateKeySample(BaseModel):
    side: Literal["source", "target"]
    primary_key: dict[str, str | None]
    occurrences: int


class NullCheck(BaseModel):
    side: Literal["source", "target"]
    column: str
    null_count: int
    total: int
    blocking: bool


class FormatCheck(BaseModel):
    side: Literal["source", "target"]
    column: str
    rule: str
    failure_count: int
    total: int


class AggregateMismatch(BaseModel):
    dimension: dict[str, str | None]
    metric: str
    source_value: str | None = None
    target_value: str | None = None


class SchemaDrift(BaseModel):
    only_in_source: list[str] = Field(default_factory=list)
    only_in_target: list[str] = Field(default_factory=list)
    type_mismatches: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ValidationReport(BaseModel):
    status: Literal["PASS", "FAIL", "ERROR"]
    status_reasons: list[str] = Field(default_factory=list)
    source_table: str
    target_table: str
    primary_keys: list[str]
    timestamp_column: str
    timestamp_alignment: str
    source_domain_published_ts: str | None = None
    target_domain_published_ts: str | None = None
    source_max_domain_published_ts: str | None = None
    target_max_domain_published_ts: str | None = None
    execution_ts: str
    source_count: int = 0
    target_count: int = 0
    mismatch_count: int = 0
    missing_in_target_count: int = 0
    missing_in_source_count: int = 0
    value_mismatch_count: int = 0
    duplicate_keys_source: int = 0
    duplicate_keys_target: int = 0
    duplicate_rows_source: int = 0
    duplicate_rows_target: int = 0
    null_pk_source: int = 0
    null_pk_target: int = 0
    schema_drift: SchemaDrift = Field(default_factory=SchemaDrift)
    null_checks: list[NullCheck] = Field(default_factory=list)
    format_checks: list[FormatCheck] = Field(default_factory=list)
    deserialization_failures: list[FormatCheck] = Field(default_factory=list)
    aggregate_mismatches: list[AggregateMismatch] = Field(default_factory=list)
    mismatched_keys: list[MismatchRow] = Field(default_factory=list)
    duplicate_key_samples: list[DuplicateKeySample] = Field(default_factory=list)
    quarantine_preview: list[MismatchRow] = Field(default_factory=list)
    source_profile: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    step_errors: dict[str, str] = Field(default_factory=dict)
    dqx_checks: list[dict[str, Any]] = Field(default_factory=list)


class SqlStep(BaseModel):
    name: str
    sql: str
    critical: bool = False


class ValidationOutcome(BaseModel):
    report: ValidationReport | None = None
    sql_steps: list[SqlStep]
    dqx_checks: list[dict[str, Any]]
    notebook: str | None = None
    schema_drift: SchemaDrift
    warnings: list[str] = Field(default_factory=list)


class ExecuteNotebookRequest(BaseModel):
    code: str = Field(min_length=1)
    notebook_name: str | None = None
    base_parameters: dict[str, str] = Field(default_factory=dict)
    wait: bool = True
    timeout_seconds: int = 180


class RunStatus(BaseModel):
    run_id: int
    life_cycle_state: str | None = None
    result_state: str | None = None
    state_message: str | None = None
    run_page_url: str | None = None
    notebook_path: str | None = None
    output: str | None = None
    error: str | None = None
    finished: bool = False


class UploadResult(BaseModel):
    table: str
    volume_path: str
    columns: list[ColumnSpec]
    row_count: int | None = None
    sheet: str | None = None
    original_filename: str
    added_timestamp_column: bool = False
