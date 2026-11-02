import { CADENCES, DOMAINS, STAGES, cadenceLabel, stageLabel } from "../domains";
import type { Approval, Health, PipelineRecord, RunRecord, ValidationReport, Workspace } from "../types";

export default function DeskView({
  pipelines,
  approvals,
  report,
  runs,
  health,
  model,
  onOpen,
}: {
  pipelines: PipelineRecord[];
  approvals: Approval[];
  report: ValidationReport | null;
  runs: RunRecord[];
  health: Health | null;
  model: "openai" | "qwen" | null;
  onOpen: (workspace: Workspace) => void;
}) {
  const pending = approvals.filter((item) => item.status === "pending");
  const late =
    report?.source_domain_published_ts &&
    report.target_domain_published_ts &&
    report.source_domain_published_ts !== report.target_domain_published_ts;
  const attention = attentionItems(pipelines, pending, report, runs);
  const recentRuns = [...runs].sort((a, b) => b.submittedAt.localeCompare(a.submittedAt)).slice(0, 5);
  const stageMax = Math.max(1, ...STAGES.map((stage) => pipelines.filter((pipeline) => pipeline.stage === stage.id).length));

  return (
    <div className="h-full overflow-auto px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-6xl">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Operations desk</p>
        <h1 className="mt-2 font-display text-3xl">What is published, and what is waiting.</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Engineering files the pipeline, analytics reads the quality snapshot, and ETL watches the run window. Chat
          still drafts the notebook. Nothing here copies rows or creates a destination.
        </p>

        <div className="mt-6 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
          <Stat label="Pipelines" value={pipelines.length} action="Open pipelines" onClick={() => onOpen("pipelines")} />
          <Stat label="Approvals waiting" value={pending.length} action="Review" onClick={() => onOpen("approvals")} />
          <Stat
            label="Latest quality"
            value={report?.status ?? "None"}
            action="Open quality"
            onClick={() => onOpen("quality")}
          />
          <Stat label="Notebook runs" value={runs.length} action="Open runs" onClick={() => onOpen("runs")} />
        </div>

        <section className="mt-8">
          <div className="mb-3 flex items-end justify-between gap-3">
            <h2 className="font-display text-lg">Needs attention</h2>
            <p className="text-xs text-muted">
              {health == null
                ? "Checking connections"
                : health.agent_runtime === "langgraph"
                  ? "Agent graph is on"
                  : "Agent graph is off"}
              {health?.databricks_token_configured ? " · Catalog connected" : " · Catalog needs a token"}
              {model === "qwen" ? " · Local model" : model === "openai" ? " · OpenAI" : ""}
            </p>
          </div>
          {attention.length === 0 ? (
            <p className="text-sm leading-6 text-muted">
              Nothing is waiting. Filed pipelines, the latest quality check, and notebook runs are below.
            </p>
          ) : (
            <ul className="divide-y divide-line overflow-hidden rounded-2xl border border-line bg-panel">
              {attention.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    onClick={() => onOpen(item.workspace)}
                    className="flex w-full items-center justify-between gap-4 px-4 py-3 text-left hover:bg-panel-2"
                  >
                    <span>
                      <span className="block text-sm">{item.title}</span>
                      <span className="mt-0.5 block text-xs text-muted">{item.detail}</span>
                    </span>
                    <span className="shrink-0 text-xs text-accent">Open</span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <div className="mt-8 grid gap-4 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
          <section className="rounded-2xl border border-line bg-panel p-4">
            <div className="flex items-end justify-between gap-3">
              <h2 className="font-display text-lg">Quality snapshot</h2>
              <button type="button" className="text-xs text-accent" onClick={() => onOpen("quality")}>
                Open quality
              </button>
            </div>
            {!report ? (
              <p className="mt-2 text-sm leading-6 text-muted">
                A quality check records row counts, mismatches, and the source and destination domain_published_ts. A
                difference means one side is late.
              </p>
            ) : (
              <>
                <p className="mt-2 truncate font-mono text-[11px] text-muted">
                  {report.source_table} → {report.target_table}
                </p>
                <div className="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-3">
                  <Metric label="Source rows" value={report.source_count} />
                  <Metric label="Destination rows" value={report.target_count} />
                  <Metric label="Mismatches" value={report.mismatch_count} warn={report.mismatch_count > 0} />
                  <Metric label="Missing in destination" value={report.missing_in_target_count} />
                  <Metric label="Missing in source" value={report.missing_in_source_count} />
                  <Metric label="Value mismatches" value={report.value_mismatch_count} />
                </div>
                <div className="mt-4 space-y-1 text-sm">
                  <p>
                    <span className="text-muted">Source published: </span>
                    {report.source_domain_published_ts ?? "n/a"}
                  </p>
                  <p>
                    <span className="text-muted">Destination published: </span>
                    {report.target_domain_published_ts ?? "n/a"}
                  </p>
                  <p className={late ? "text-amber" : "text-teal"}>
                    {late ? "The two snapshots do not match." : "The snapshots match, or one side has no timestamp yet."}
                  </p>
                </div>
              </>
            )}
          </section>

          <section className="rounded-2xl border border-line bg-panel p-4">
            <div className="flex items-end justify-between gap-3">
              <h2 className="font-display text-lg">Latest notebook runs</h2>
              <button type="button" className="text-xs text-accent" onClick={() => onOpen("runs")}>
                Open runs
              </button>
            </div>
            {recentRuns.length === 0 ? (
              <p className="mt-2 text-sm leading-6 text-muted">
                A submitted notebook shows its Databricks result here. Starting a run still happens from Runs or from
                chat after the notebook is reviewed.
              </p>
            ) : (
              <ul className="mt-3 space-y-3">
                {recentRuns.map((run) => (
                  <li key={run.id} className="flex items-start justify-between gap-3 text-sm">
                    <span className="min-w-0">
                      <span className="block truncate">{run.title || "Notebook"}</span>
                      <span className="mt-0.5 block text-xs text-muted">{formatWhen(run.submittedAt)}</span>
                    </span>
                    <span className={`shrink-0 text-xs ${runTone(run)}`}>{runLabel(run)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </div>

        <div className="mt-4 grid gap-4 lg:grid-cols-2">
          <section className="rounded-2xl border border-line bg-panel p-4">
            <h2 className="font-display text-lg">Pipeline stages</h2>
            <ul className="mt-3 space-y-3">
              {STAGES.map((stage) => {
                const count = pipelines.filter((pipeline) => pipeline.stage === stage.id).length;
                return (
                  <li key={stage.id}>
                    <div className="flex justify-between gap-3 text-sm">
                      <span>{stage.label}</span>
                      <span className="text-muted">{count}</span>
                    </div>
                    <div className="mt-1 h-1.5 overflow-hidden rounded-full bg-panel-2">
                      <div className="h-full rounded-full bg-lime" style={{ width: `${(count / stageMax) * 100}%` }} />
                    </div>
                  </li>
                );
              })}
            </ul>
          </section>
          <section className="rounded-2xl border border-line bg-panel p-4">
            <h2 className="font-display text-lg">Run windows</h2>
            <ul className="mt-3 space-y-2 text-sm">
              {CADENCES.map((cadence) => {
                const count = pipelines.filter((pipeline) => pipeline.cadence === cadence.id).length;
                return (
                  <li key={cadence.id} className="flex justify-between gap-3">
                    <span>{cadence.label}</span>
                    <span className="text-muted">{count}</span>
                  </li>
                );
              })}
            </ul>
            <p className="mt-3 text-xs leading-5 text-muted">
              Windows are the contract with the publishing team. The notebook still runs when someone submits it.
            </p>
          </section>
        </div>

        <section className="mt-8">
          <h2 className="font-display text-lg">Domains</h2>
          <div className="mt-3 grid gap-3 md:grid-cols-2 xl:grid-cols-5">
            {DOMAINS.map((domain) => {
              const owned = pipelines.filter((pipeline) => pipeline.domain === domain.id);
              return (
                <article key={domain.id} className="rounded-2xl border border-line bg-panel p-3">
                  <h3 className="font-display text-sm">{domain.label}</h3>
                  <p className="mt-1 text-xs leading-5 text-muted">{domain.detail}</p>
                  <ul className="mt-3 space-y-2">
                    {owned.length === 0 && <li className="text-xs text-muted">No pipeline filed here yet.</li>}
                    {owned.slice(0, 4).map((pipeline) => (
                      <li key={pipeline.id} className="text-xs leading-5">
                        <span className="text-paper">{pipeline.name}</span>
                        <span className="mt-0.5 block text-muted">
                          {stageLabel(pipeline.stage)} · {cadenceLabel(pipeline.cadence)}
                        </span>
                      </li>
                    ))}
                  </ul>
                </article>
              );
            })}
          </div>
        </section>
      </div>
    </div>
  );
}

function attentionItems(
  pipelines: PipelineRecord[],
  pending: Approval[],
  report: ValidationReport | null,
  runs: RunRecord[],
) {
  const items: { id: string; title: string; detail: string; workspace: Workspace }[] = [];
  for (const item of pending) {
    items.push({
      id: item.id,
      title: item.kind === "run" ? "Copy is waiting for review" : "Destination is waiting for approval",
      detail: item.destination,
      workspace: "approvals",
    });
  }
  if (report?.status === "FAIL" || report?.status === "ERROR") {
    items.push({
      id: "quality",
      title: report.status === "FAIL" ? "Latest quality check failed" : "Latest quality check errored",
      detail: `${report.source_table} → ${report.target_table}`,
      workspace: "quality",
    });
  }
  for (const pipeline of pipelines.filter((item) => item.status === "needs_approval")) {
    items.push({
      id: pipeline.id,
      title: "Pipeline is waiting to be filed",
      detail: pipeline.destination,
      workspace: "pipelines",
    });
  }
  const unassigned = pipelines.filter((pipeline) => pipeline.domain === "unassigned");
  if (unassigned.length > 0) {
    items.push({
      id: "unassigned",
      title: unassigned.length === 1 ? "1 chat draft has no domain" : `${unassigned.length} chat drafts have no domain`,
      detail: "Assign a domain from Pipelines.",
      workspace: "pipelines",
    });
  }
  for (const run of runs.filter((item) => item.finished && item.result_state && item.result_state !== "SUCCESS").slice(0, 3)) {
    items.push({
      id: run.id,
      title: `Notebook run ${run.result_state}`,
      detail: run.title || "Notebook",
      workspace: "runs",
    });
  }
  return items;
}

function Metric({ label, value, warn = false }: { label: string; value: number; warn?: boolean }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-[0.12em] text-muted">{label}</p>
      <p className={`mt-1 font-display text-xl ${warn ? "text-amber" : ""}`}>{value.toLocaleString()}</p>
    </div>
  );
}

function runLabel(run: RunRecord) {
  if (!run.finished) return run.life_cycle_state ?? "Running";
  return run.result_state ?? run.life_cycle_state ?? "Unknown";
}

function runTone(run: RunRecord) {
  if (!run.finished) return "text-amber";
  if (run.result_state === "SUCCESS") return "text-teal";
  return "text-coral";
}

function formatWhen(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
}

function Stat({
  label,
  value,
  action,
  onClick,
}: {
  label: string;
  value: string | number;
  action: string;
  onClick: () => void;
}) {
  return (
    <button type="button" onClick={onClick} className="rounded-2xl border border-line bg-panel px-4 py-4 text-left hover:border-lime">
      <p className="text-[11px] uppercase tracking-[0.14em] text-muted">{label}</p>
      <p className="mt-2 font-display text-2xl">{value}</p>
      <p className="mt-2 text-xs text-accent">{action}</p>
    </button>
  );
}
