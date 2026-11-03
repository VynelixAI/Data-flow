
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

  useEffect(() => {
    setTab(focus);
  }, [focus]);

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
    <aside className="flex h-full w-full min-h-0 min-w-0 flex-col overflow-hidden border-t border-line md:border-t-0 md:border-l">

      {/* Inspector tabs */}
      <div className="flex w-full min-w-0 shrink-0 gap-1 overflow-x-auto border-b border-line px-3 py-2">
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() => setTab(id)}
            className={`shrink-0 whitespace-nowrap rounded-full px-3 py-1.5 text-sm ${
              tab === id
                ? "bg-paper text-ink"
                : "text-muted"
            }`}
          >
            {label}
          </button>
        ))}
      </div>

      {/* Inspector content */}
      <div className="flex w-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden">

        {tab === "code" && (
          <div className="h-full min-h-0 min-w-0 flex-1 overflow-auto">
            <CodeViewer
              code={code}
              title={codeTitle}
              sql={sql}
            />
          </div>
        )}

        {tab === "validation" && (
          <div className="h-full min-h-0 min-w-0 flex-1 overflow-auto">
            <ValidationReportView
              report={report}
              warnings={warnings}
              sql={sql}
              running={validating}
              onRun={onRunValidation}
            />
          </div>
        )}

        {tab === "quarantine" && (
          <div className="h-full min-h-0 min-w-0 flex-1 overflow-auto overscroll-contain px-3 py-4 sm:px-4">
            <div className="w-full min-w-0">
              <QuarantineTable report={report} />
            </div>
          </div>
        )}

        {tab === "run" && (
          <div className="h-full min-h-0 min-w-0 flex-1 overflow-auto">
            <ExecutionStatus
              run={run}
              code={code}
              running={executing}
              onExecute={onExecute}
            />
          </div>
        )}

      </div>
    </aside>
  );
}
