import { CADENCES, DOMAINS, cadenceLabel, stageLabel } from "../domains";
import type { Approval, Health, PipelineRecord, RunRecord, ValidationReport, Workspace } from "../types";

export default function DeskView({
  pipelines,
  approvals,
  report,
  runs,
  health,
  onOpen,
}: {
  pipelines: PipelineRecord[];
  approvals: Approval[];
  report: ValidationReport | null;
  runs: RunRecord[];
  health: Health | null;
  onOpen: (workspace: Workspace) => void;
}) {
  const pending = approvals.filter((item) => item.status === "pending");
  const late =
    report?.source_domain_published_ts &&
    report.target_domain_published_ts &&
    report.source_domain_published_ts !== report.target_domain_published_ts;

  return (
    <div className="h-full overflow-auto px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-6xl">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Operations desk</p>
        <h1 className="mt-2 font-display text-3xl">What is published, and what is waiting.</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Catalog objects, quality checks, and notebook runs share one desk. New tables and files are written only
          after someone approves them. Chat is where the agent drafts the work.
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
            <h2 className="font-display text-lg">Domains</h2>
            <p className="text-xs text-muted">
              {health == null
                ? "Checking connections"
                : health.agent_runtime === "langgraph"
                  ? "Agent graph is on"
                  : "Agent graph is off"}
              {health?.databricks_token_configured ? " · Catalog connected" : " · Catalog needs a token"}
            </p>
          </div>
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-5">
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

        <div className="mt-8 grid gap-4 lg:grid-cols-2">
          <section className="rounded-2xl border border-line bg-panel p-4">
            <h2 className="font-display text-lg">Published timestamps</h2>
            {!report ? (
              <p className="mt-2 text-sm leading-6 text-muted">
                A quality check records the source and destination domain_published_ts. A difference means one side is
                late, which is how yesterday&apos;s store file gets missed.
              </p>
            ) : (
              <div className="mt-3 space-y-2 text-sm">
                <p>
                  <span className="text-muted">Source: </span>
                  {report.source_domain_published_ts ?? "n/a"}
                </p>
                <p>
                  <span className="text-muted">Destination: </span>
                  {report.target_domain_published_ts ?? "n/a"}
                </p>
                <p className={late ? "text-amber" : "text-teal"}>
                  {late ? "The two snapshots do not match." : "The snapshots match, or one side has no timestamp yet."}
                </p>
              </div>
            )}
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
              Windows are the contract with store operations. The notebook still runs when someone submits it.
            </p>
          </section>
        </div>

        {pipelines.some((pipeline) => pipeline.domain === "unassigned") && (
          <p className="mt-6 text-sm text-muted">
            {pipelines.filter((pipeline) => pipeline.domain === "unassigned").length} chat draft
            {pipelines.filter((pipeline) => pipeline.domain === "unassigned").length === 1 ? " is" : "s are"} not filed
            under a domain yet. Open Pipelines and assign one.
          </p>
        )}
      </div>
    </div>
  );
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
