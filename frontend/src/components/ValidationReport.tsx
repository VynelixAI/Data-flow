import { useState, type ReactNode } from "react";
import type { MismatchRow, ValidationReport } from "../types";
import { Empty } from "./CodeViewer";

function StatusPill({ status }: { status: ValidationReport["status"] }) {
  const tone =
    status === "PASS" ? "bg-teal text-ink" : status === "FAIL" ? "bg-coral text-ink" : "bg-amber text-ink";
  return <span className={`rounded-full px-3 py-1 font-display text-sm font-semibold ${tone}`}>{status}</span>;
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-2xl border border-line bg-panel-2 px-3 py-3">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="mt-1 font-display text-xl">{value}</p>
    </div>
  );
}

function keyLabel(row: MismatchRow) {
  return Object.entries(row.primary_key)
    .map(([key, value]) => `${key}=${value ?? "null"}`)
    .join(", ");
}

export function QuarantineTable({ rows }: { rows: MismatchRow[] }) {
  if (!rows.length) {
    return <p className="text-sm text-muted">No quarantined keys in this sample.</p>;
  }
  return (
    <div className="overflow-auto rounded-2xl border border-line">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead className="bg-panel-2 text-xs uppercase tracking-wider text-muted">
          <tr>
            <th className="px-3 py-2 font-medium">Primary key</th>
            <th className="px-3 py-2 font-medium">Type</th>
            <th className="px-3 py-2 font-medium">Column differences</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${keyLabel(row)}-${index}`} className="border-t border-line align-top">
              <td className="px-3 py-2 font-mono text-xs">{keyLabel(row)}</td>
              <td className="px-3 py-2">{row.mismatch_type.replaceAll("_", " ")}</td>
              <td className="px-3 py-2 text-xs leading-5 text-muted">
                {row.column_diffs.length
                  ? row.column_diffs.map((diff) => (
                      <div key={diff.column}>
                        <span className="text-paper">{diff.column}</span>: {diff.source_value ?? "null"} →{" "}
                        {diff.target_value ?? "null"}
                      </div>
                    ))
                  : "Key missing on one side"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function ValidationReportView({
  report,
  warnings,
  onRun,
  running,
}: {
  report: ValidationReport | null;
  warnings: string[];
  onRun: (payload: Record<string, unknown>) => void;
  running: boolean;
}) {
  const [source, setSource] = useState("");
  const [target, setTarget] = useState("");
  const [keys, setKeys] = useState("order_id");
  const [compare, setCompare] = useState("");
  const [timestampColumn, setTimestampColumn] = useState("domain_published_ts");
  const [alignment, setAlignment] = useState("latest_each");
  const [softDelete, setSoftDelete] = useState("");
  const [execute, setExecute] = useState(false);
  const [advanced, setAdvanced] = useState(false);

  return (
    <div className="flex h-full min-h-0 flex-col overflow-auto">
      <form
        className="space-y-3 border-b border-line px-4 py-4"
        onSubmit={(event) => {
          event.preventDefault();
          onRun({
            source_table: source.trim(),
            target_table: target.trim(),
            primary_keys: keys.split(",").map((item) => item.trim()).filter(Boolean),
            compare_columns: compare.trim() ? compare.split(",").map((item) => item.trim()).filter(Boolean) : null,
            timestamp_column: timestampColumn.trim() || "domain_published_ts",
            timestamp_alignment: alignment,
            soft_delete_column: softDelete.trim() || null,
            execute,
            include_notebook: true,
          });
        }}
      >
        <div className="flex items-center justify-between">
          <p className="font-display text-sm">Direct validation</p>
          <button type="button" className="text-xs text-muted underline" onClick={() => setAdvanced((value) => !value)}>
            {advanced ? "Hide options" : "Options"}
          </button>
        </div>
        <div className="grid gap-2 sm:grid-cols-2">
          <Field label="Source table or volume file" value={source} onChange={setSource} placeholder="catalog.schema.table or /Volumes/.../file.csv" />
          <Field label="Destination table or volume file" value={target} onChange={setTarget} placeholder="catalog.schema.table or /Volumes/.../file.csv" />
        </div>
        <Field label="Primary keys" value={keys} onChange={setKeys} />
        {advanced && (
          <div className="grid gap-2">
            <Field label="Compare columns" value={compare} onChange={setCompare} />
            <div className="grid gap-2 sm:grid-cols-2">
              <Field label="Timestamp column" value={timestampColumn} onChange={setTimestampColumn} />
              <label className="block text-xs text-muted">
                Alignment
                <select
                  className="mt-1 w-full rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
                  value={alignment}
                  onChange={(event) => setAlignment(event.target.value)}
                >
                  <option value="latest_each">Latest snapshot on each side</option>
                  <option value="common_max">Shared published timestamp</option>
                </select>
              </label>
            </div>
            <Field label="Soft-delete column" value={softDelete} onChange={setSoftDelete} />
            <label className="flex items-center gap-2 text-sm text-muted">
              <input type="checkbox" checked={execute} onChange={(event) => setExecute(event.target.checked)} />
              Execute on the SQL warehouse
            </label>
          </div>
        )}
        <button
          type="submit"
          disabled={running}
          className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {running ? "Running…" : execute ? "Run validation" : "Generate validation SQL"}
        </button>
      </form>

      {!report ? (
        <Empty
          title="No report yet"
          body="Generate SQL here, or ask the chat to validate a source against a target. Execution needs a SQL warehouse."
        />
      ) : (
        <div className="space-y-4 px-4 py-4">
          <div className="flex flex-wrap items-center gap-3">
            <StatusPill status={report.status} />
            <p className="text-sm text-muted">
              {report.source_table} → {report.target_table}
            </p>
          </div>
          {report.status_reasons.length > 0 && (
            <ul className="space-y-1 text-sm text-paper">
              {report.status_reasons.map((reason) => (
                <li key={reason}>{reason}</li>
              ))}
            </ul>
          )}
          <div className="grid grid-cols-2 gap-2 lg:grid-cols-4">
            <Metric label="Source rows" value={report.source_count} />
            <Metric label="Target rows" value={report.target_count} />
            <Metric label="Mismatches" value={report.mismatch_count} />
            <Metric label="Value diffs" value={report.value_mismatch_count} />
          </div>
          <div className="grid gap-2 text-sm sm:grid-cols-2">
            <p>
              <span className="text-muted">Source {report.timestamp_column}: </span>
              {report.source_domain_published_ts ?? "n/a"}
            </p>
            <p>
              <span className="text-muted">Target {report.timestamp_column}: </span>
              {report.target_domain_published_ts ?? "n/a"}
            </p>
            <p>
              <span className="text-muted">Missing in target: </span>
              {report.missing_in_target_count}
            </p>
            <p>
              <span className="text-muted">Missing in source: </span>
              {report.missing_in_source_count}
            </p>
            <p>
              <span className="text-muted">Duplicate keys: </span>
              {report.duplicate_keys_source} / {report.duplicate_keys_target}
            </p>
            <p>
              <span className="text-muted">Null keys: </span>
              {report.null_pk_source} / {report.null_pk_target}
            </p>
            <p className="sm:col-span-2 text-xs text-muted">Audited {report.execution_ts}</p>
          </div>
          <Section title="Schema drift">
            <p className="text-sm text-muted">
              Only in source: {report.schema_drift.only_in_source.join(", ") || "none"}
            </p>
            <p className="text-sm text-muted">
              Only in target: {report.schema_drift.only_in_target.join(", ") || "none"}
            </p>
            {report.schema_drift.type_mismatches.map((item) => (
              <p key={item.column} className="text-sm">
                {item.column}: {item.source_type} vs {item.target_type} ({item.severity})
              </p>
            ))}
          </Section>
          <Section title="Mismatched keys">
            <QuarantineTable rows={report.mismatched_keys} />
          </Section>
          {report.aggregate_mismatches.length > 0 && (
            <Section title="Aggregates">
              {report.aggregate_mismatches.slice(0, 30).map((item, index) => (
                <p key={`${item.metric}-${index}`} className="text-sm">
                  {Object.entries(item.dimension)
                    .map(([key, value]) => `${key}=${value ?? "null"}`)
                    .join(", ")}{" "}
                  {item.metric}: {item.source_value ?? "null"} vs {item.target_value ?? "null"}
                </p>
              ))}
            </Section>
          )}
          {(report.format_checks.some((item) => item.failure_count) ||
            report.deserialization_failures.some((item) => item.failure_count)) && (
            <Section title="Formats and deserialization">
              {[...report.format_checks, ...report.deserialization_failures]
                .filter((item) => item.failure_count > 0)
                .map((item) => (
                  <p key={`${item.side}-${item.column}-${item.rule}`} className="text-sm">
                    {item.side}.{item.column} failed {item.rule} ({item.failure_count})
                  </p>
                ))}
            </Section>
          )}
          {warnings.length > 0 && (
            <Section title="Warnings">
              {warnings.map((warning) => (
                <p key={warning} className="text-sm text-amber">
                  {warning}
                </p>
              ))}
            </Section>
          )}
        </div>
      )}
    </div>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
}) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <input
        className="mt-1 w-full rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper outline-none focus:border-lime"
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="font-display text-sm">{title}</h3>
      {children}
    </section>
  );
}
