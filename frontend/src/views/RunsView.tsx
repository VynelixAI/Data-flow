import ExecutionStatus from "../components/ExecutionStatus";
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
    <div className="grid h-full min-h-0 lg:grid-cols-[minmax(280px,360px)_minmax(0,1fr)]">
      <div className="overflow-auto border-b border-line px-4 py-5 lg:border-b-0 lg:border-r">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Runs</p>
        <h1 className="mt-2 font-display text-2xl">Notebook jobs</h1>
        <p className="mt-2 text-sm leading-6 text-muted">
          A run uploads the notebook and submits it. The result state is what finance and store ops will ask for when a
          morning publish is late.
        </p>
        <ul className="mt-4 space-y-2">
          {runs.length === 0 && <li className="text-sm text-muted">No notebook has been submitted in this session.</li>}
          {runs.map((run) => (
            <li key={run.id} className="rounded-2xl border border-line px-3 py-3 text-sm">
              <p>{run.title}</p>
              <p className="mt-1 text-xs text-muted">
                {run.life_cycle_state ?? "submitted"}
                {run.result_state ? ` / ${run.result_state}` : ""} · {new Date(run.submittedAt).toLocaleString()}
              </p>
            </li>
          ))}
        </ul>
      </div>
      <ExecutionStatus run={latest} code={code} running={executing} onExecute={onExecute} />
    </div>
  );
}
