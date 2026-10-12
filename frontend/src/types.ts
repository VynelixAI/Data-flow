export type ColumnSpec = {
  name: string;
  data_type: string;
  nullable?: boolean;
};

export type UploadedTable = {
  table: string;
  volume_path: string;
  columns: ColumnSpec[];
  row_count: number | null;
  original_filename?: string;
};

export type ColumnDiff = {
  column: string;
  source_value: string | null;
  target_value: string | null;
};

export type MismatchRow = {
  primary_key: Record<string, string | null>;
  mismatch_type: "MISSING_IN_TARGET" | "MISSING_IN_SOURCE" | "VALUE_MISMATCH";
  column_diffs: ColumnDiff[];
};

export type ValidationReport = {
  status: "PASS" | "FAIL" | "ERROR";
  status_reasons: string[];
  source_table: string;
  target_table: string;
  primary_keys: string[];
  timestamp_column: string;
  timestamp_alignment: string;
  source_domain_published_ts: string | null;
  target_domain_published_ts: string | null;
  source_max_domain_published_ts: string | null;
  target_max_domain_published_ts: string | null;
  execution_ts: string;
  source_count: number;
  target_count: number;
  mismatch_count: number;
  missing_in_target_count: number;
  missing_in_source_count: number;
  value_mismatch_count: number;
  duplicate_keys_source: number;
  duplicate_keys_target: number;
  null_pk_source: number;
  null_pk_target: number;
  schema_drift: {
    only_in_source: string[];
    only_in_target: string[];
    type_mismatches: { column: string; source_type: string; target_type: string; severity: string }[];
    warnings: string[];
  };
  null_checks: { side: string; column: string; null_count: number; total: number; blocking: boolean }[];
  format_checks: { side: string; column: string; rule: string; failure_count: number; total: number }[];
  deserialization_failures: { side: string; column: string; rule: string; failure_count: number; total: number }[];
  aggregate_mismatches: { dimension: Record<string, string | null>; metric: string; source_value: string | null; target_value: string | null }[];
  mismatched_keys: MismatchRow[];
  quarantine_preview: MismatchRow[];
  warnings: string[];
  step_errors: Record<string, string>;
};

export type SqlStep = {
  name: string;
  sql: string;
  critical?: boolean;
};

export type RunStatus = {
  run_id: number;
  life_cycle_state: string | null;
  result_state: string | null;
  state_message: string | null;
  run_page_url: string | null;
  notebook_path: string | null;
  output: string | null;
  error: string | null;
  finished: boolean;
};

export type Confirmation = {
  message: string;
  destination: string;
};

export type Artifact = {
  kind: "code" | "validation" | "execution" | "quarantine" | "confirmation";
  title?: string;
  language?: string;
  content?: string;
  sql?: SqlStep[];
  report?: ValidationReport;
  execution?: RunStatus;
  warnings?: string[];
  message?: string;
  destination?: string;
};

export type ToolStep = {
  id: string;
  name: string;
  label: string;
  status: "running" | "done" | "error";
};

export type ChatTurn = {
  id: string;
  role: "user" | "assistant";
  content: string;
  steps: ToolStep[];
};

export type Health = {
  status: string;
  databricks_host: string | null;
  databricks_host_configured: boolean;
  databricks_token_configured: boolean;
  databricks_warehouse_configured: boolean;
  openai_configured: boolean;
  agent_runtime?: string;
};

export type Workspace = "desk" | "chat" | "pipelines" | "catalog" | "quality" | "runs" | "approvals";

export type DomainId = "stores" | "merchandising" | "supply" | "customer" | "finance" | "unassigned";

export type Cadence = "manual" | "after_close" | "morning_publish" | "intraday" | "weekly";

export type Stage = "dev" | "stage" | "prod";

export type PipelineRecord = {
  id: string;
  name: string;
  domain: DomainId;
  stage: Stage;
  cadence: Cadence;
  source: string;
  destination: string;
  primaryKeys: string;
  writeMode: "merge" | "overwrite" | "append";
  code: string | null;
  updatedAt: string;
  status: "draft" | "needs_approval" | "ready";
};

export type Approval = {
  id: string;
  destination: string;
  message: string;
  status: "pending" | "approved" | "declined";
  createdAt: string;
  stage?: Stage;
  threadId?: string;
  draft?: Omit<PipelineRecord, "id" | "code" | "updatedAt" | "status">;
};

export type RunRecord = RunStatus & {
  id: string;
  submittedAt: string;
  title: string;
};

export type StreamEvent = {
  type: "token" | "tool_start" | "tool_end" | "artifact" | "error" | "done" | "hold";
  thread_id?: string;
  content?: string;
  name?: string;
  label?: string;
  ok?: boolean;
  message?: string;
  kind?: Artifact["kind"];
  title?: string;
  language?: string;
  sql?: SqlStep[];
  report?: ValidationReport;
  execution?: RunStatus;
  warnings?: string[];
  destination?: string;
};
