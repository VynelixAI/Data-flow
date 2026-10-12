import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { ChatTurn, Confirmation, UploadedTable } from "../types";
import FileUpload from "./FileUpload";

const SUGGESTIONS = [
  "Copy the source and destination I name, then validate every shared column. Primary key is order_id.",
  "Search the catalog for the dataset I describe and use that exact table or file path.",
  "If the destination file is missing, ask me before creating it, then copy the rows and validate them.",
];

export default function Chat({
  messages,
  streaming,
  uploads,
  confirmation,
  onSend,
  onStop,
  onReset,
  onUploaded,
}: {
  messages: ChatTurn[];
  streaming: boolean;
  uploads: UploadedTable[];
  confirmation: Confirmation | null;
  onSend: (text: string, resume?: { approved: boolean; destination: string }) => void;
  onStop: () => void;
  onReset: () => void;
  onUploaded: (table: UploadedTable) => void;
}) {
  const [draft, setDraft] = useState("");
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    scroller.current?.scrollTo({ top: scroller.current.scrollHeight });
  }, [messages, streaming]);

  function submit() {
    const text = draft.trim();
    if (!text || streaming) return;
    setDraft("");
    onSend(text);
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div ref={scroller} className="min-h-0 flex-1 overflow-auto px-4 py-6 sm:px-8">
        {messages.length === 0 ? (
          <div className="mx-auto flex min-h-full max-w-2xl flex-col justify-center">
            <p className="text-[11px] uppercase tracking-[0.18em] text-accent">Agent</p>
            <p className="mt-2 font-display text-3xl leading-tight">Copy a source. Prove every shared column.</p>
            <p className="mt-3 max-w-xl text-base leading-7 text-muted">
              Ask for a pipeline, a profile, or a source-to-target check. The agent pauses when a destination is missing.
              Notebooks, quality, and runs open in the other tabs.
            </p>
            <div className="mt-6 space-y-2">
              {SUGGESTIONS.map((prompt) => (
                <button
                  key={prompt}
                  type="button"
                  onClick={() => onSend(prompt)}
                  className="block w-full rounded-2xl border border-line bg-panel px-4 py-3 text-left text-sm leading-6 hover:border-lime"
                >
                  {prompt}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="mx-auto max-w-2xl space-y-6">
            {messages.map((message) => (
              <article key={message.id} className={message.role === "user" ? "flex justify-end" : ""}>
                <div
                  className={
                    message.role === "user"
                      ? "max-w-[90%] rounded-3xl rounded-br-md bg-lime px-4 py-3 text-white"
                      : "max-w-full"
                  }
                >
                  {message.steps.length > 0 && (
                    <div className="mb-3 flex flex-wrap gap-2">
                      {message.steps.map((step) => (
                        <span
                          key={step.id}
                          className={`rounded-full border px-2.5 py-1 text-xs ${
                            step.status === "running"
                              ? "border-lime text-lime"
                              : step.status === "error"
                                ? "border-coral text-coral"
                                : "border-line text-muted"
                          }`}
                        >
                          {step.label}
                          {step.status === "running" ? "…" : ""}
                        </span>
                      ))}
                    </div>
                  )}
                  {message.role === "user" ? (
                    <p className="whitespace-pre-wrap text-sm leading-6">{message.content}</p>
                  ) : (
                    <div className="prose-dataflow text-sm leading-6">
                      {message.content ? (
                        <ReactMarkdown remarkPlugins={[remarkGfm]}>{message.content}</ReactMarkdown>
                      ) : (
                        streaming && <span className="text-muted">Working…</span>
                      )}
                    </div>
                  )}
                </div>
              </article>
            ))}
          </div>
        )}
      </div>
      <div className="border-t border-line px-4 py-4 sm:px-8">
        <div className="mx-auto max-w-2xl">
          {confirmation && (
            <div className="mb-3 rounded-2xl border border-lime/40 bg-panel px-4 py-3">
              <p className="text-sm leading-6">{confirmation.message}</p>
              <p className="mt-1 font-mono text-xs text-accent">{confirmation.destination}</p>
              <div className="mt-3 flex gap-2">
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() =>
                    onSend(
                      `Yes, create the Unity Catalog destination ${confirmation.destination}. Generate the pipeline with confirm_create_destination true.`,
                      { approved: true, destination: confirmation.destination },
                    )
                  }
                  className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                >
                  Create it
                </button>
                <button
                  type="button"
                  disabled={streaming}
                  onClick={() =>
                    onSend(`Do not create ${confirmation.destination}. Leave that destination alone.`, {
                      approved: false,
                      destination: confirmation.destination,
                    })
                  }
                  className="rounded-full border border-line px-4 py-2 text-sm disabled:opacity-40"
                >
                  Don't create
                </button>
              </div>
            </div>
          )}
          {uploads.length > 0 && (
            <div className="mb-2 flex flex-wrap gap-2">
              {uploads.map((upload) => (
                <span key={upload.table} className="rounded-full bg-panel-2 px-3 py-1 font-mono text-[11px] text-accent">
                  {upload.table}
                </span>
              ))}
            </div>
          )}
          <div className="rounded-3xl border border-line bg-panel p-3">
            <textarea
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !event.shiftKey) {
                  event.preventDefault();
                  submit();
                }
              }}
              rows={3}
              placeholder="Ask for a pipeline, a column profile, or a source-to-target check"
              className="w-full resize-none bg-transparent text-sm leading-6 outline-none placeholder:text-muted"
            />
            <div className="mt-2 flex flex-wrap items-center gap-2">
              <div className="flex items-center gap-3">
                <FileUpload onUploaded={onUploaded} disabled={streaming} />
                <button type="button" onClick={onReset} className="whitespace-nowrap text-xs text-muted">
                  New conversation
                </button>
              </div>
              {streaming ? (
                <button type="button" onClick={onStop} className="ml-auto rounded-full border border-line px-4 py-2 text-sm">
                  Stop
                </button>
              ) : (
                <button
                  type="button"
                  onClick={submit}
                  disabled={!draft.trim()}
                  className="ml-auto rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
                >
                  Send
                </button>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
