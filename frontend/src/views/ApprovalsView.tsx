import { stageLabel } from "../domains";
import type { Approval } from "../types";

export default function ApprovalsView({
  approvals,
  onDecide,
}: {
  approvals: Approval[];
  onDecide: (approval: Approval, approved: boolean) => void;
}) {
  const pending = approvals.filter((item) => item.status === "pending");
  const closed = approvals.filter((item) => item.status !== "pending");
  return (
    <div className="h-full overflow-auto px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-3xl">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Approvals</p>
        <h1 className="mt-2 font-display text-3xl">Nothing new is written until someone says so.</h1>
        <p className="mt-2 text-sm leading-6 text-muted">
          A missing destination table or CSV/Excel file waits here. Approving lets the notebook create it. Declining
          leaves the catalog untouched.
        </p>
        <div className="mt-6 space-y-3">
          {pending.length === 0 && (
            <p className="rounded-2xl border border-line bg-panel px-4 py-4 text-sm text-muted">No destinations are waiting.</p>
          )}
          {pending.map((approval) => (
            <article key={approval.id} className="rounded-2xl border border-lime/40 bg-panel px-4 py-4">
              <p className="text-sm leading-6">{approval.message}</p>
              <p className="mt-2 font-mono text-xs text-accent">{approval.destination}</p>
              {approval.stage && <p className="mt-1 text-xs text-muted">{stageLabel(approval.stage)}</p>}
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  onClick={() => onDecide(approval, true)}
                  className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white"
                >
                  {approval.draft ? "Approve and generate" : "Create it"}
                </button>
                <button
                  type="button"
                  onClick={() => onDecide(approval, false)}
                  className="rounded-full border border-line px-4 py-2 text-sm"
                >
                  Don't create
                </button>
              </div>
            </article>
          ))}
        </div>
        {closed.length > 0 && (
          <section className="mt-8">
            <h2 className="font-display text-lg">Decided</h2>
            <ul className="mt-3 space-y-2">
              {closed.map((approval) => (
                <li key={approval.id} className="flex items-center justify-between gap-3 rounded-2xl border border-line px-4 py-3 text-sm">
                  <span className="font-mono text-xs">{approval.destination}</span>
                  <span className="text-muted">{approval.status}</span>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}
