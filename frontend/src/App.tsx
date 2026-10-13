import { useEffect, useState } from "react";
import { apiError } from "./apiError";
import Chat from "./components/Chat";
import Inspector from "./components/Inspector";
import Sidebar from "./components/Sidebar";
import SplitPane from "./components/SplitPane";
import ValidationReportView from "./components/ValidationReport";
import type {
  Approval,
  Artifact,
  ChatTurn,
  Confirmation,
  Health,
  PipelineRecord,
  ResumePayload,
  RunRecord,
  RunStatus,
  StreamEvent,
  UploadedTable,
  ValidationReport,
  Workspace,
} from "./types";
import ApprovalsView from "./views/ApprovalsView";
import CatalogView from "./views/CatalogView";
import DeskView from "./views/DeskView";
import PipelinesView from "./views/PipelinesView";
import RunsView from "./views/RunsView";

const STORAGE_KEY = "dataflow-agent-session-v1";

type Session = {
  workspace: Workspace;
  messages: ChatTurn[];
  uploads: UploadedTable[];
  code: string | null;
  codeTitle: string;
  sql: { name: string; sql: string }[];
  report: ValidationReport | null;
  warnings: string[];
  run: RunStatus | null;
  runs: RunRecord[];
  confirmation: Confirmation | null;
  agentThreadId: string | null;
  pipelines: PipelineRecord[];
  approvals: Approval[];
  pipelineSeed: { source?: string; destination?: string } | null;
};

const EMPTY: Session = {
  workspace: "desk",
  messages: [],
  uploads: [],
  code: null,
  codeTitle: "",
  sql: [],
  report: null,
  warnings: [],
  run: null,
  runs: [],
  confirmation: null,
  agentThreadId: null,
  pipelines: [],
  approvals: [],
  pipelineSeed: null,
};

function loadSession(): Session {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return EMPTY;
    const parsed = JSON.parse(raw) as Partial<Session>;
    return {
      ...EMPTY,
      ...parsed,
      runs: parsed.runs ?? [],
      pipelines: parsed.pipelines ?? [],
      approvals: parsed.approvals ?? [],
      workspace: parsed.workspace ?? "desk",
      code: null,
      codeTitle: "",
      sql: [],
      report: null,
      warnings: [],
      run: null,
    };
  } catch {
    return EMPTY;
  }
}

function newId() {
  return crypto.randomUUID();
}

export default function App() {
  const [session, setSession] = useState<Session>(loadSession);
  const [inspectorTab, setInspectorTab] = useState<"code" | "validation" | "quarantine" | "run">("code");
  const [streaming, setStreaming] = useState(false);
  const [controller, setController] = useState<AbortController | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [banner, setBanner] = useState<string | null>(null);
  const [validating, setValidating] = useState(false);
  const [executing, setExecuting] = useState(false);

  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(session));
    } catch {
      // A large notebook can exceed storage. The live screen still works.
    }
  }, [session]);

  useEffect(() => {
    void fetch("/api/health")
      .then((response) => response.json())
      .then(setHealth)
      .catch(() => setHealth(null));
  }, []);

  function applyArtifact(artifact: Artifact) {
    setSession((current) => {
      const next = { ...current };
      if (artifact.kind === "confirmation" && artifact.destination && artifact.message) {
        const kind = artifact.confirmationKind === "run" ? "run" : "create";
        next.confirmation = {
          destination: artifact.destination,
          message: artifact.message,
          kind,
          notes: artifact.notes || [],
          reviewId: artifact.reviewId,
        };
        const approval: Approval = {
          id: newId(),
          destination: artifact.destination,
          message: artifact.message,
          status: "pending",
          createdAt: new Date().toISOString(),
          threadId: current.agentThreadId ?? undefined,
          kind,
          notes: artifact.notes || [],
        };
        next.approvals = [
          approval,
          ...current.approvals.filter((item) => !(item.destination === approval.destination && item.status === "pending")),
        ];
      }
      if (artifact.kind === "code" && artifact.content) {
        next.code = artifact.content;
        next.codeTitle = artifact.title || "Generated notebook";
        next.sql = artifact.sql ?? [];
        next.confirmation = null;
        if ((artifact.title || "").includes("→")) {
          const [source, destination] = (artifact.title || "").split("→").map((part) => part.trim());
          if (source && destination) {
            const record: PipelineRecord = {
              id: newId(),
              name: artifact.title || `${source} to ${destination}`,
              domain: "unassigned",
              stage: "dev",
              cadence: "manual",
              source,
              destination,
              primaryKeys: "",
              writeMode: "merge",
              code: artifact.content,
              updatedAt: new Date().toISOString(),
              status: "needs_approval",
            };
            next.pipelines = [record, ...current.pipelines.filter((item) => item.destination !== destination)];
            next.approvals = current.approvals.map((item) =>
              item.destination === destination && item.status === "pending" ? { ...item, status: "approved" } : item,
            );
          }
        }
      }
      if ((artifact.kind === "validation" || artifact.kind === "quarantine") && artifact.report) {
        next.report = artifact.report;
        next.warnings = artifact.warnings || artifact.report.warnings || [];
      }
      if (artifact.kind === "execution" && artifact.execution) {
        next.run = artifact.execution;
        const record: RunRecord = {
          ...artifact.execution,
          id: newId(),
          submittedAt: new Date().toISOString(),
          title: current.codeTitle || "Notebook",
        };
        next.runs = [record, ...current.runs];
      }
      return next;
    });
    if (artifact.kind === "code") setInspectorTab("code");
    if (artifact.kind === "validation") setInspectorTab("validation");
    if (artifact.kind === "quarantine") setInspectorTab("quarantine");
    if (artifact.kind === "execution") setInspectorTab("run");
  }

  async function send(
    text: string,
    resume?: ResumePayload,
    threadId?: string | null,
  ) {
    const user: ChatTurn = { id: newId(), role: "user", content: text, steps: [] };
    const assistantId = newId();
    const assistant: ChatTurn = { id: assistantId, role: "assistant", content: "", steps: [] };
    const history = [...session.messages, user];
    const typed =
      resume ??
      (session.confirmation
        ? replyDecision(text, session.confirmation.destination) ??
          (session.confirmation.kind === "run" && text.trim()
            ? {
                approved: true,
                destination: session.confirmation.destination,
                comment: text.trim(),
                action: "revise" as const,
              }
            : undefined)
        : undefined);
    const resumeThread = threadId ?? session.agentThreadId;
    const clearCode = !typed;
    setSession((current) => ({
      ...current,
      workspace: "chat",
      messages: [...history, assistant],
      confirmation: null,
      ...(clearCode
        ? { code: null, codeTitle: "", sql: [], report: null, warnings: [], run: null }
        : {}),
      approvals: typed
        ? current.approvals.map((item) =>
            item.destination === typed.destination && item.status === "pending"
              ? { ...item, status: typed.approved ? "approved" : "declined" }
              : item,
          )
        : current.approvals,
    }));
    setBanner(null);
    const abort = new AbortController();
    setController(abort);
    setStreaming(true);
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        signal: abort.signal,
        body: JSON.stringify({
          messages: history.map((message) => ({ role: message.role, content: message.content })),
          thread_id: typed ? resumeThread : null,
          resume: typed ?? null,
          context: {
            uploaded_tables: session.uploads.map((upload) => ({
              table: upload.table,
              volume_path: upload.volume_path,
              columns: upload.columns,
              row_count: upload.row_count,
            })),
          },
        }),
      });
      if (!response.ok || !response.body) {
        const payload = await response.json().catch(() => ({ error: response.statusText }));
        throw new Error(payload.error || "Chat failed");
      }
      await readSse(response, (event) => {
        if (event.type === "token" && event.content) {
          setSession((current) => ({
            ...current,
            messages: current.messages.map((message) =>
              message.id === assistantId ? { ...message, content: message.content + event.content } : message,
            ),
          }));
        }
        if (event.type === "tool_start") {
          setSession((current) => ({
            ...current,
            messages: current.messages.map((message) =>
              message.id === assistantId
                ? {
                    ...message,
                    steps: [
                      ...message.steps,
                      {
                        id: newId(),
                        name: event.name || "tool",
                        label: event.label || event.name || "Tool",
                        status: "running",
                      },
                    ],
                  }
                : message,
            ),
          }));
        }
        if (event.type === "tool_end") {
          setSession((current) => ({
            ...current,
            messages: current.messages.map((message) => {
              if (message.id !== assistantId) return message;
              const steps = [...message.steps];
              const index = [...steps].reverse().findIndex((step) => step.name === event.name && step.status === "running");
              if (index >= 0) {
                const actual = steps.length - 1 - index;
                steps[actual] = { ...steps[actual], status: event.ok ? "done" : "error" };
              }
              return { ...message, steps };
            }),
          }));
        }
        if (event.type === "artifact" && event.kind) {
          applyArtifact(event as Artifact);
        }
        if (event.type === "hold" && event.thread_id) {
          const thread = event.thread_id;
          setSession((current) => ({
            ...current,
            agentThreadId: thread,
            approvals: current.approvals.map((item) =>
              item.status === "pending" && !item.threadId && !item.draft ? { ...item, threadId: thread } : item,
            ),
          }));
        }
        if (event.type === "error" && event.message) {
          setBanner(event.message);
          setSession((current) => ({
            ...current,
            messages: current.messages.map((message) =>
              message.id === assistantId && !message.content
                ? { ...message, content: event.message || "The agent hit an error." }
                : message,
            ),
          }));
        }
      });
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      const message = error instanceof Error ? error.message : "Chat failed";
      setBanner(message);
      setSession((current) => ({
        ...current,
        messages: current.messages.map((messageTurn) =>
          messageTurn.id === assistantId ? { ...messageTurn, content: messageTurn.content || message } : messageTurn,
        ),
      }));
    } finally {
      setStreaming(false);
      setController(null);
    }
  }

  async function runValidation(payload: Record<string, unknown>) {
    setValidating(true);
    setBanner(null);
    try {
      const response = await fetch("/api/validate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(apiError(body, "Validation failed"));
      if (body.notebook) {
        applyArtifact({
          kind: "code",
          title: "Source-to-target validation",
          content: body.notebook,
          language: "python",
          sql: body.sql_steps,
        });
      } else if (body.sql_steps) {
        setSession((current) => ({ ...current, sql: body.sql_steps }));
      }
      if (body.report) {
        applyArtifact({ kind: "validation", report: body.report, warnings: body.warnings });
        applyArtifact({ kind: "quarantine", report: body.report, warnings: body.warnings });
      } else {
        setInspectorTab("code");
        setSession((current) => ({ ...current, warnings: body.warnings || [], sql: body.sql_steps || current.sql }));
      }
    } catch (error) {
      setBanner(error instanceof Error ? error.message : "Validation failed");
    } finally {
      setValidating(false);
    }
  }

  async function executeCurrent() {
    if (!session.code) return;
    setExecuting(true);
    setBanner(null);
    try {
      const response = await fetch("/api/notebooks/execute", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ code: session.code, wait: false }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "Notebook submission failed");
      applyArtifact({ kind: "execution", execution: body as RunStatus });
      setSession((current) => ({ ...current, workspace: current.workspace === "chat" ? "chat" : "runs" }));
    } catch (error) {
      setBanner(error instanceof Error ? error.message : "Notebook submission failed");
    } finally {
      setExecuting(false);
    }
  }

  async function generateApproved(approval: Approval) {
    if (!approval.draft) return;
    setBanner(null);
    try {
      const draft = approval.draft;
      const response = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_table: draft.source,
          target_table: draft.destination,
          primary_keys: draft.primaryKeys.split(",").map((item) => item.trim()).filter(Boolean),
          write_mode: draft.writeMode,
          confirm_create_destination: true,
          notes: `${draft.stage} · ${draft.cadence} · ${draft.name}`,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "Could not generate the notebook");
      const record: PipelineRecord = {
        ...draft,
        id: newId(),
        code: body.notebook,
        updatedAt: new Date().toISOString(),
        status: "ready",
      };
      setSession((current) => ({
        ...current,
        workspace: "pipelines",
        code: body.notebook,
        codeTitle: record.name,
        pipelines: [
          record,
          ...current.pipelines.filter((item) => !(item.destination === record.destination && item.status === "needs_approval")),
        ],
        approvals: current.approvals.map((item) => (item.id === approval.id ? { ...item, status: "approved" } : item)),
      }));
    } catch (error) {
      setBanner(error instanceof Error ? error.message : "Could not generate the notebook");
    }
  }

  function decide(approval: Approval, approved: boolean) {
    if (approval.draft) {
      if (!approved) {
        setSession((current) => ({
          ...current,
          approvals: current.approvals.map((item) => (item.id === approval.id ? { ...item, status: "declined" } : item)),
        }));
        return;
      }
      void generateApproved(approval);
      return;
    }
    const running = approval.kind === "run";
    const text = approved
      ? running
        ? `Run the reviewed notebook and copy into ${approval.destination}.`
        : `Yes, create the Unity Catalog destination ${approval.destination}. Generate the pipeline with confirm_create_destination true.`
      : running
        ? `Do not run the copy to ${approval.destination}.`
        : `Do not create ${approval.destination}. Leave that destination alone.`;
    void send(text, { approved, destination: approval.destination, action: approved ? "approve" : "decline" }, approval.threadId);
  }

  const pendingApprovals = session.approvals.filter((item) => item.status === "pending").length;

  return (
    <div className="flex h-full flex-col">
      <header className="flex items-center justify-between gap-4 border-b border-line px-4 py-3 sm:px-6">
        <div>
          <p className="font-display text-base leading-none">DataFlow</p>
          <p className="mt-1 text-xs text-muted">Catalog, quality, and runs</p>
        </div>
        <div className="flex flex-wrap items-center justify-end gap-2 text-[11px]">
          <ThemeToggle />
          <Pill ok={health?.agent_runtime === "langgraph"} label="Agent graph" />
          <Pill ok={health?.databricks_token_configured} label="Databricks" />
          <Pill ok={health?.databricks_warehouse_configured} label="Warehouse" />
          <Pill ok={health?.openai_configured} label="OpenAI" />
        </div>
      </header>
      {banner && <div className="border-b border-line bg-banner px-4 py-2 text-sm text-amber">{banner}</div>}
      <div className="flex min-h-0 flex-1 flex-col md:flex-row">
        <Sidebar
          workspace={session.workspace}
          pendingApprovals={pendingApprovals}
          onSelect={(workspace) => setSession((current) => ({ ...current, workspace }))}
        />
        <main className="flex h-full min-h-0 min-w-0 flex-1 flex-col overflow-hidden">
          {session.workspace === "desk" && (
            <DeskView
              pipelines={session.pipelines}
              approvals={session.approvals}
              report={session.report}
              runs={session.runs}
              health={health}
              onOpen={(workspace) => setSession((current) => ({ ...current, workspace }))}
            />
          )}
          {session.workspace === "chat" && (
            <SplitPane
              storageKey="dataflow-chat-split"
              initial={0.54}
              first={
              <Chat
                messages={session.messages}
                streaming={streaming}
                uploads={session.uploads}
                confirmation={session.confirmation}
                onSend={(text, resume) => void send(text, resume)}
                onStop={() => controller?.abort()}
                onReset={() =>
                  setSession((current) => ({
                    ...current,
                    messages: [],
                    confirmation: null,
                    agentThreadId: null,
                    code: null,
                    codeTitle: "",
                    sql: [],
                    report: null,
                    warnings: [],
                    run: null,
                  }))
                }
                onUploaded={(upload) => {
                  setSession((current) => ({ ...current, uploads: [...current.uploads, upload] }));
                  setBanner(`Created ${upload.table}. Mention it in the next message and the agent can use it.`);
                }}
              />
              }
              second={
              <Inspector
                code={session.code}
                codeTitle={session.codeTitle}
                sql={session.sql}
                report={session.report}
                warnings={session.warnings}
                run={session.run}
                validating={validating}
                executing={executing}
                focus={inspectorTab}
                onRunValidation={(payload) => void runValidation(payload)}
                onExecute={() => void executeCurrent()}
              />
              }
            />
          )}
          {session.workspace === "pipelines" && (
            <PipelinesView
              pipelines={session.pipelines}
              seed={session.pipelineSeed}
              onSave={(record) =>
                setSession((current) => ({
                  ...current,
                  code: record.code,
                  codeTitle: record.name,
                  pipelines: [
                    record,
                    ...current.pipelines.filter(
                      (item) => !(item.destination === record.destination && item.status === "needs_approval"),
                    ),
                  ],
                  approvals: current.approvals.map((item) =>
                    item.destination === record.destination && item.status === "pending"
                      ? { ...item, status: "approved" }
                      : item,
                  ),
                }))
              }
              onNeedApproval={(input) => {
                const approval: Approval = {
                  id: newId(),
                  destination: input.destination,
                  message: input.message,
                  status: "pending",
                  createdAt: new Date().toISOString(),
                  stage: input.stage,
                  draft: input.draft,
                };
                const draftRecord: PipelineRecord = {
                  ...input.draft,
                  id: newId(),
                  code: null,
                  updatedAt: new Date().toISOString(),
                  status: "needs_approval",
                };
                setSession((current) => ({
                  ...current,
                  approvals: [
                    approval,
                    ...current.approvals.filter(
                      (item) => !(item.destination === approval.destination && item.status === "pending"),
                    ),
                  ],
                  pipelines: [
                    draftRecord,
                    ...current.pipelines.filter((item) => item.destination !== draftRecord.destination),
                  ],
                }));
              }}
              onSelectCode={(code, title) => setSession((current) => ({ ...current, code, codeTitle: title }))}
            />
          )}
          {session.workspace === "catalog" && (
            <CatalogView
              onUse={(fullName, role) =>
                setSession((current) => ({
                  ...current,
                  workspace: "pipelines",
                  pipelineSeed:
                    role === "destination"
                      ? { source: current.pipelineSeed?.source, destination: fullName }
                      : { source: fullName, destination: current.pipelineSeed?.destination },
                }))
              }
            />
          )}
          {session.workspace === "quality" && (
            <div className="flex h-full min-h-0 flex-col">
              <div className="border-b border-line px-4 py-4 sm:px-8">
                <p className="text-xs uppercase tracking-[0.16em] text-muted">Quality</p>
                <h1 className="mt-1 font-display text-2xl">Source against destination</h1>
                <p className="mt-1 max-w-2xl text-sm text-muted">
                  Reconcile the latest published snapshot. This check reads both sides. It does not create the destination.
                </p>
              </div>
              <div className="min-h-0 flex-1">
                <ValidationReportView
                  report={session.report}
                  warnings={session.warnings}
                  sql={session.sql}
                  running={validating}
                  onRun={(payload) => void runValidation(payload)}
                />
              </div>
            </div>
          )}
          {session.workspace === "runs" && (
            <RunsView
              runs={session.runs}
              code={session.code}
              executing={executing}
              onExecute={() => void executeCurrent()}
            />
          )}
          {session.workspace === "approvals" && <ApprovalsView approvals={session.approvals} onDecide={decide} />}
        </main>
      </div>
    </div>
  );
}

function replyDecision(text: string, destination: string): ResumePayload | undefined {
  const value = text.trim().toLowerCase().replace(/[.!]+$/g, "");
  if (/^(y|yes|yeah|yep|ok|okay|create|create it|do it|go ahead|run|run it)\b/.test(value)) {
    return { approved: true, destination, action: "approve" };
  }
  if (/^(n|no|nope|don't|dont|do not)\b/.test(value)) {
    return { approved: false, destination, action: "decline" };
  }
  return undefined;
}

function ThemeToggle() {
  const [theme, setTheme] = useState<"dark" | "light">(() =>
    document.documentElement.dataset.theme === "light" ? "light" : "dark",
  );

  function choose(next: "dark" | "light") {
    document.documentElement.dataset.theme = next;
    localStorage.setItem("dataflow-theme", next);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", next === "light" ? "#f3f6fb" : "#05070d");
    setTheme(next);
  }

  return (
    <div className="flex rounded-full border border-line p-0.5">
      {(["dark", "light"] as const).map((option) => (
        <button
          key={option}
          type="button"
          onClick={() => choose(option)}
          className={`rounded-full px-2.5 py-1 capitalize ${theme === option ? "bg-lime text-white" : "text-muted"}`}
        >
          {option}
        </button>
      ))}
    </div>
  );
}

function Pill({ ok, label }: { ok?: boolean; label: string }) {
  return (
    <span className={`rounded-full px-2 py-1 ${ok ? "bg-ok text-teal" : "bg-panel-2 text-muted"}`}>{label}</span>
  );
}

async function readSse(response: Response, onEvent: (event: StreamEvent) => void) {
  const reader = response.body?.getReader();
  if (!reader) return;
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";
    for (const chunk of chunks) {
      const data = chunk
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trim())
        .join("\n");
      if (!data) continue;
      onEvent(JSON.parse(data) as StreamEvent);
    }
  }
}
