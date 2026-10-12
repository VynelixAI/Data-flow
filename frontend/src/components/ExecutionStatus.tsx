import type { RunStatus } from "../types";
import { Empty } from "./CodeViewer";

export default function ExecutionStatus({
  run,
  code,
  onExecute,
  running,
}: {
  run: RunStatus | null;
  code: string | null;
  onExecute: () => void;
  running: boolean;
}) {
  return (
    <div className="flex h-full min-h-0 flex-col overflow-auto px-4 py-4">
      <button
        type="button"
        disabled={!code || running}
        onClick={onExecute}
        className="mb-4 w-fit rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
      >
        {running ? "Submitting…" : "Run this notebook on Databricks"}
      </button>
      {!run ? (
        <Empty
          title="No run yet"
          body="Executing uploads the notebook into your workspace folder and submits a serverless job, or a cluster job if DATABRICKS_CLUSTER_ID is set."
        />
      ) : (
        <div className="space-y-3 text-sm">
          <p className="font-display text-lg">Run {run.run_id}</p>
          <p>
            <span className="text-muted">State: </span>
            {run.life_cycle_state ?? "unknown"}
            {run.result_state ? ` / ${run.result_state}` : ""}
          </p>
          {run.state_message && <p className="text-muted">{run.state_message}</p>}
          {run.notebook_path && (
            <p className="break-all font-mono text-xs text-muted">{run.notebook_path}</p>
          )}
          {run.run_page_url && (
            <a className="text-lime underline" href={run.run_page_url} target="_blank" rel="noreferrer">
              Open in Databricks
            </a>
          )}
          {run.error && <pre className="overflow-auto whitespace-pre-wrap rounded-2xl bg-panel-2 p-3 text-coral">{run.error}</pre>}
          {run.output && (
            <pre className="overflow-auto whitespace-pre-wrap rounded-2xl bg-panel-2 p-3 font-mono text-xs leading-5">
              {run.output}
            </pre>
          )}
        </div>
      )}
    </div>
  );
}
