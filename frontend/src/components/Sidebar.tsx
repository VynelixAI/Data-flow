import type { Workspace } from "../types";

const ITEMS: { id: Workspace; label: string; hint: string }[] = [
  { id: "desk", label: "Desk", hint: "What needs attention" },
  { id: "chat", label: "Chat", hint: "Agent for both teams" },
  { id: "pipelines", label: "Pipelines", hint: "Contracts and notebooks" },
  { id: "catalog", label: "Catalog", hint: "Tables and files" },
  { id: "quality", label: "Quality", hint: "Source to destination" },
  { id: "runs", label: "Runs", hint: "Notebook jobs" },
  { id: "approvals", label: "Approvals", hint: "Create destinations" },
];

export default function Sidebar({
  workspace,
  pendingApprovals,
  onSelect,
}: {
  workspace: Workspace;
  pendingApprovals: number;
  onSelect: (workspace: Workspace) => void;
}) {
  return (
    <nav className="flex shrink-0 gap-1 overflow-auto border-b border-line bg-panel/80 px-2 py-2 backdrop-blur md:w-60 md:flex-col md:gap-1 md:overflow-hidden md:border-b-0 md:border-r md:px-3 md:py-5">
      <div className="hidden px-2 pb-5 md:block">
        <p className="font-display text-lg leading-none">DataFlow</p>
        <p className="mt-1 text-[11px] leading-4 text-muted">Catalog, quality, and runs</p>
      </div>
      {ITEMS.map((item) => {
        const active = workspace === item.id;
        const badge = item.id === "approvals" ? pendingApprovals : 0;
        return (
          <button
            key={item.id}
            type="button"
            onClick={() => onSelect(item.id)}
            className={`flex min-w-fit items-center justify-between rounded-xl px-3 py-2 text-left md:w-full ${
              active ? "bg-lime text-white" : "text-paper hover:bg-panel-2"
            }`}
          >
            <span>
              <span className="block text-sm font-medium">{item.label}</span>
              <span className={`hidden text-[11px] md:block ${active ? "text-white/75" : "text-muted"}`}>{item.hint}</span>
            </span>
            {badge > 0 && (
              <span className={`rounded-full px-2 py-0.5 text-[11px] ${active ? "bg-white/15 text-white" : "bg-amber text-ink"}`}>
                {badge}
              </span>
            )}
          </button>
        );
      })}
    </nav>
  );
}
