import { useEffect, useMemo, useState } from "react";

const KEYWORDS = new Set([
  "def",
  "class",
  "import",
  "from",
  "return",
  "if",
  "else",
  "elif",
  "for",
  "while",
  "in",
  "try",
  "except",
  "with",
  "as",
  "True",
  "False",
  "None",
  "and",
  "or",
  "not",
  "lambda",
  "raise",
  "pass",
]);

function paint(text: string) {
  if (text.trimStart().startsWith("#")) {
    return <span className="text-code-comment">{text || " "}</span>;
  }
  const parts = text.split(/("(?:\\.|[^"])*"|'(?:\\.|[^'])*'|\b[A-Za-z_][A-Za-z0-9_]*\b)/g);
  return parts.map((part, index) => {
    if (!part) return null;
    if (part.startsWith('"') || part.startsWith("'")) {
      return (
        <span key={index} className="text-code-string">
          {part}
        </span>
      );
    }
    if (KEYWORDS.has(part)) {
      return (
        <span key={index} className="text-code-keyword">
          {part}
        </span>
      );
    }
    return <span key={index}>{part}</span>;
  });
}

export default function CodeViewer({
  code,
  title,
  sql,
}: {
  code: string | null;
  title?: string;
  sql?: { name: string; sql: string }[];
}) {
  const [tab, setTab] = useState<"notebook" | "sql">("notebook");
  const [copied, setCopied] = useState(false);
  const lines = useMemo(() => (code ?? "").split("\n"), [code]);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 1400);
    return () => window.clearTimeout(timer);
  }, [copied]);

  if (!code && !(sql && sql.length)) {
    return (
      <Empty
        title="No notebook yet"
        body="The notebook stays empty until you name a source and a destination. It appears here after the agent generates it."
      />
    );
  }

  const active = tab === "sql" && sql && sql.length ? sql.map((step) => `-- ${step.name}\n${step.sql}`).join("\n") : code ?? "";

  return (
    <div className="flex h-full min-h-0 flex-1 flex-col overflow-hidden">
      <div className="flex items-center justify-between gap-3 border-b border-line px-4 py-3">
        <div className="min-w-0">
          <p className="truncate font-display text-sm font-medium">{title || "Generated notebook"}</p>
          <p className="text-xs text-muted">{active.split("\n").length} lines</p>
        </div>
        <div className="flex items-center gap-2">
          {sql && sql.length > 0 && (
            <div className="flex rounded-full border border-line p-0.5 text-xs">
              <button
                className={`rounded-full px-2.5 py-1 ${tab === "notebook" ? "bg-lime text-white" : "text-muted"}`}
                onClick={() => setTab("notebook")}
                type="button"
              >
                Notebook
              </button>
              <button
                className={`rounded-full px-2.5 py-1 ${tab === "sql" ? "bg-lime text-white" : "text-muted"}`}
                onClick={() => setTab("sql")}
                type="button"
              >
                SQL
              </button>
            </div>
          )}
          <button
            type="button"
            className="rounded-full border border-line px-3 py-1 text-xs text-paper hover:border-lime"
            onClick={() => {
              void navigator.clipboard.writeText(active);
              setCopied(true);
            }}
          >
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
      </div>
      <pre className="notebook-scroll min-h-0 flex-1 px-0 py-3 font-mono text-[12.5px] leading-6">
        {(tab === "sql" && sql ? sql.flatMap((step) => [`-- ${step.name}`, ...step.sql.split("\n"), ""]) : lines).map(
          (line, index) => (
            <div key={`${index}-${line.slice(0, 12)}`} className="grid grid-cols-[3.2rem_1fr] gap-3 px-4 hover:bg-code-hover">
              <span className="text-right text-code-gutter">{index + 1}</span>
              <code className="whitespace-pre-wrap break-words">{paint(line)}</code>
            </div>
          ),
        )}
      </pre>
    </div>
  );
}

export function Empty({ title, body }: { title: string; body: string }) {
  return (
    <div className="flex h-full items-center justify-center px-8 text-center">
      <div className="max-w-sm">
        <p className="font-display text-lg">{title}</p>
        <p className="mt-2 text-sm leading-6 text-muted">{body}</p>
      </div>
    </div>
  );
}
