import { useEffect, useMemo, useState } from "react";
import type { RunStatus, ValidationReport } from "../types";
import CodeViewer from "./CodeViewer";
import ExecutionStatus from "./ExecutionStatus";
import ValidationReportView, { QuarantineTable } from "./ValidationReport";

type InspectorTab = "code" | "validation" | "quarantine" | "run";

export default function Inspector({
  code,
  codeTitle,
  sql,
  report,
  warnings,
  run,
  validating,
  executing,
  focus,
  onRunValidation,
  onExecute,
}: {
  code: string | null;
  codeTitle: string;
  sql: { name: string; sql: string }[];
  report: ValidationReport | null;
  warnings: string[];
  run: RunStatus | null;
  validating: boolean;
  executing: boolean;
  focus: InspectorTab;
  onRunValidation: (payload: Record<string, unknown>) => void;
  onExecute: () => void;
}) {
  const [tab, setTab] = useState<InspectorTab>(focus);
  useEffect(() => setTab(focus), [focus]);
  const tabs = useMemo(
    () =>
      [
        ["code", "Code"],
        ["validation", "Validation"],
        ["quarantine", "Quarantine"],
        ["run", "Run"],
      ] as const,
    [],
  );

  return (
    <aside className="flex min-h-0 flex-col overflow-hidden border-t border-line md:border-t-0 md:border-l">
      <div className="flex gap-1 border-b border-line px-3 py-2">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={`rounded-full px-3 py-1.5 text-sm ${tab === id ? "bg-paper text-ink" : "text-muted"}`}
          >
            {label}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1">
        {tab === "code" && <CodeViewer code={code} title={codeTitle} sql={sql} />}
        {tab === "validation" && (
          <ValidationReportView report={report} warnings={warnings} running={validating} onRun={onRunValidation} />
        )}
        {tab === "quarantine" && (
          <div className="h-full overflow-auto px-4 py-4">
            <QuarantineTable rows={report?.quarantine_preview ?? []} />
          </div>
        )}
        {tab === "run" && <ExecutionStatus run={run} code={code} running={executing} onExecute={onExecute} />}
      </div>
    </aside>
  );
}
