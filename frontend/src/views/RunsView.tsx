import ExecutionStatus from "../components/ExecutionStatus";
import SplitPane from "../components/SplitPane";
import type { RunRecord } from "../types";

export default function RunsView({
  runs,
  code,
  executing,
  onExecute,
}: {
  runs: RunRecord[];
  code: string | null;
  executing: boolean;
  onExecute: () => void;
}) {
  const latest = runs[0] ?? null;

  return (
    <SplitPane
      storageKey="dataflow-runs-split"
      initial={0.4}
      first={
        <div className="h-full min-h-0 min-w-0 overflow-auto px-4 py-5">
          <p className="text-xs uppercase tracking-[0.16em] text-muted">
            Runs
          </p>

          <h1 className="mt-2 font-display text-2xl">
            Notebook jobs
          </h1>

          <p className="mt-2 text-sm leading-6 text-muted">
            A run uploads the notebook and submits it.
            Track execution status and job results.
          </p>

          <ul className="mt-4 space-y-2">
            {runs.length === 0 && (
              <li className="text-sm text-muted">
                No notebook has been submitted in this session.
              </li>
            )}

            {runs.map((run) => (
              <li
                key={run.id}
                className="min-w-0 rounded-2xl border border-line px-3 py-3 text-sm"
              >
                <p className="break-words">{run.title}</p>

                <p className="mt-1 break-words text-xs text-muted">
                  {run.life_cycle_state ?? "submitted"}
                  {run.result_state
                    ? ` / ${run.result_state}`
                    : ""}
                  {" · "}
                  {new Date(run.submittedAt).toLocaleString()}
                </p>
              </li>
            ))}
          </ul>
        </div>
      }
      second={
        <div className="h-full min-h-0 min-w-0 overflow-auto">
          <ExecutionStatus
            run={latest}
            code={code}
            running={executing}
            onExecute={onExecute}
          />
        </div>
      }
    />
  );
}