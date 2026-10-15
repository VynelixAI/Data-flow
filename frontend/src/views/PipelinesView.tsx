import { useEffect, useState, type ReactNode } from "react";
import { apiError } from "../apiError";
import CatalogSearch from "../components/CatalogSearch";
import CodeViewer from "../components/CodeViewer";
import SplitPane from "../components/SplitPane";
import { CADENCES, DOMAINS, STAGES, cadenceLabel, stageLabel } from "../domains";
import type { Cadence, DomainId, PipelineRecord, Stage } from "../types";

const PIPELINE_DRAFT_KEY = "dataflow-pipeline-form-draft";

type PipelineFormDraft = {
  name: string;
  domain: DomainId;
  stage: Stage;
  cadence: Cadence;
  source: string;
  destination: string;
  keys: string;
  writeMode: "merge" | "overwrite" | "append";
};

const DEFAULT_PIPELINE_DRAFT: PipelineFormDraft = {
  name: "Store inventory publish",
  domain: "stores",
  stage: "dev",
  cadence: "after_close",
  source: "main.bronze.store_inventory",
  destination: "main.silver.store_inventory",
  keys: "store_id,sku",
  writeMode: "merge",
};

function loadPipelineDraft(): PipelineFormDraft {
  try {
    const saved = localStorage.getItem(PIPELINE_DRAFT_KEY);
    if (saved) {
      return {
        ...DEFAULT_PIPELINE_DRAFT,
        ...JSON.parse(saved),
      };
    }
  } catch {
    // Use defaults if the saved draft cannot be read.
  }

  return DEFAULT_PIPELINE_DRAFT;
}

export default function PipelinesView({
  pipelines,
  seed,
  onSave,
  onNeedApproval,
  onSelectCode,
}: {
  pipelines: PipelineRecord[];
  seed: { source?: string; destination?: string } | null;
  onSave: (pipeline: PipelineRecord) => void;
  onNeedApproval: (approval: {
    destination: string;
    message: string;
    stage: Stage;
    draft: Omit<PipelineRecord, "id" | "code" | "updatedAt" | "status">;
  }) => void;
  onSelectCode: (code: string, title: string) => void;
}) {
  
const [initialDraft] = useState(loadPipelineDraft);

const [name, setName] = useState(initialDraft.name);
const [domain, setDomain] = useState<DomainId>(initialDraft.domain);
const [stage, setStage] = useState<Stage>(initialDraft.stage);
const [cadence, setCadence] = useState<Cadence>(initialDraft.cadence);
const [source, setSource] = useState(initialDraft.source);
const [destination, setDestination] = useState(initialDraft.destination);
const [keys, setKeys] = useState(initialDraft.keys);
const [writeMode, setWriteMode] =
  useState<"merge" | "overwrite" | "append">(initialDraft.writeMode);

  const [selected, setSelected] = useState<string | null>(pipelines[0]?.id ?? null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingDest, setPendingDest] = useState<string | null>(null);

  useEffect(() => {
    if (!seed) return;
    if (seed.source) setSource(seed.source);
    if (seed.destination) setDestination(seed.destination);
  }, [seed]);
  
useEffect(() => {
  const draft: PipelineFormDraft = {
    name,
    domain,
    stage,
    cadence,
    source,
    destination,
    keys,
    writeMode,
  };

  try {
    localStorage.setItem(
      PIPELINE_DRAFT_KEY,
      JSON.stringify(draft)
    );
  } catch {
    // Keep the form usable if storage fails.
  }
}, [
  name,
  domain,
  stage,
  cadence,
  source,
  destination,
  keys,
  writeMode,
]);

  const current = pipelines.find((pipeline) => pipeline.id === selected) ?? null;

  async function inspect(name: string) {
    const response = await fetch(`/api/uc/inspect?name=${encodeURIComponent(name)}`);
    const body = await response.json();
    if (!response.ok) throw new Error(apiError(body, "Could not inspect Unity Catalog"));
    return body as { name: string; exists: boolean | null; checked: boolean; kind: string };
  }

  async function generate(confirmCreate: boolean) {
    setBusy(true);
    setError(null);
    try {
      const sourceName = source.trim();
      const destinationName = destination.trim();
      const primaryKeys = keys.split(",").map((item) => item.trim()).filter(Boolean);
      if (!sourceName || !destinationName || primaryKeys.length === 0) {
        setError("Source, destination, and at least one primary key are required.");
        return;
      }
      const sourceInfo = await inspect(sourceName);
      if (sourceInfo.checked && sourceInfo.exists === false) {
        setError(`Source ${sourceInfo.name} is not in Unity Catalog.`);
        return;
      }
      const destinationInfo = await inspect(destinationName);
      const missing = destinationInfo.checked && destinationInfo.exists === false;
      const unknown = !destinationInfo.checked;
      if ((missing || unknown) && !confirmCreate) {
        const message = missing
          ? `${destinationName} is not in Unity Catalog (${destinationInfo.kind.replace("_", " ")}). Create it before the pipeline writes?`
          : `Could not verify ${destinationName}. Create it if it is missing?`;
        setPendingDest(destinationName);
        onNeedApproval({
          destination: destinationName,
          message,
          stage,
          draft: {
            name: name.trim() || `${sourceName} to ${destinationName}`,
            domain,
            stage,
            cadence,
            source: sourceName,
            destination: destinationName,
            primaryKeys: primaryKeys.join(", "),
            writeMode,
          },
        });
        return;
      }
      const response = await fetch("/api/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          source_table: sourceName,
          target_table: destinationName,
          primary_keys: primaryKeys,
          write_mode: writeMode,
          confirm_create_destination: confirmCreate,
          notes: `${stageLabel(stage)} · ${cadenceLabel(cadence)} · ${name.trim() || "Pipeline"}`,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(apiError(body, "Could not generate the notebook"));
      const record: PipelineRecord = {
        id: crypto.randomUUID(),
        name: name.trim() || `${sourceName} to ${destinationName}`,
        domain,
        stage,
        cadence,
        source: sourceName,
        destination: destinationName,
        primaryKeys: primaryKeys.join(", "),
        writeMode,
        code: body.notebook,
        updatedAt: new Date().toISOString(),
        status: "ready",
      };
      setPendingDest(null);
      setSelected(record.id);
      onSave(record);
      onSelectCode(body.notebook, record.name);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Could not generate the notebook");
    } finally {
      setBusy(false);
    }
  }

  return (
    <SplitPane
      storageKey="dataflow-pipeline-split"
      initial={0.38}
      wideAt={1024}
      first={
      <div className="h-full overflow-auto px-4 py-5">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Pipeline contract</p>
        <h1 className="mt-2 font-display text-2xl">File the copy before it runs.</h1>
        <p className="mt-2 text-sm leading-6 text-muted">
          A pipeline names the domain, the run window, and the Unity Catalog source and destination. Production
          destinations that do not exist stop here for approval.
        </p>
        <div className="mt-4">
          <CatalogSearch
            onPick={(name, role) => {
              if (role === "source") setSource(name);
              else setDestination(name);
            }}
          />
        </div>
        <form
          className="mt-4 space-y-3"
          onSubmit={(event) => {
            event.preventDefault();
            void generate(false);
          }}
        >
          <Field label="Name" value={name} onChange={setName} />
          <div className="grid grid-cols-2 gap-2">
            <Select label="Domain" value={domain} onChange={(value) => setDomain(value as DomainId)}>
              {DOMAINS.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </Select>
            <Select label="Stage" value={stage} onChange={(value) => setStage(value as Stage)}>
              {STAGES.map((item) => (
                <option key={item.id} value={item.id}>
                  {item.label}
                </option>
              ))}
            </Select>
          </div>
          <Select label="Run window" value={cadence} onChange={(value) => setCadence(value as Cadence)}>
            {CADENCES.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </Select>
          <Field label="Source table or volume file" value={source} onChange={setSource} />
          <Field label="Destination table or volume file" value={destination} onChange={setDestination} />
          <Field label="Primary keys" value={keys} onChange={setKeys} />
          <Select label="Write mode" value={writeMode} onChange={(value) => setWriteMode(value as typeof writeMode)}>
            <option value="merge">Merge</option>
            <option value="overwrite">Overwrite</option>
            <option value="append">Append</option>
          </Select>
          {error && <p className="text-sm text-amber">{error}</p>}
          {pendingDest && (
            <div className="rounded-2xl border border-lime/40 bg-panel-2 px-3 py-3">
              <p className="text-sm leading-6">
                {destination.trim()} needs an explicit create. Approving writes the notebook with creation enabled. The
                object is still created only when that notebook runs.
              </p>
              <button
                type="button"
                disabled={busy}
                onClick={() => void generate(true)}
                className="mt-3 rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
              >
                Approve and generate
              </button>
            </div>
          )}
          <button
            type="submit"
            disabled={busy}
            className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >
            {busy ? "Checking catalog…" : "Check and generate"}
          </button>
        </form>
        <ul className="mt-6 space-y-2">
          {pipelines.map((pipeline) => (
            <li key={pipeline.id}>
              <button
                type="button"
                onClick={() => {
                  setSelected(pipeline.id);
                  if (pipeline.code) onSelectCode(pipeline.code, pipeline.name);
                }}
                className={`w-full rounded-2xl border px-3 py-3 text-left ${
                  selected === pipeline.id ? "border-lime bg-panel-2" : "border-line"
                }`}
              >
                <span className="block text-sm">{pipeline.name}</span>
                <span className="mt-1 block font-mono text-[11px] text-muted">
                  {pipeline.source} → {pipeline.destination}
                </span>
                <span className="mt-1 block text-[11px] text-muted">
                  {stageLabel(pipeline.stage)} · {cadenceLabel(pipeline.cadence)} · {pipeline.status.replace("_", " ")}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>
      }
      second={
      <div className="flex h-full min-h-0 flex-col">
        <CodeViewer code={current?.code ?? null} title={current?.name ?? "Notebook"} sql={[]} />
      </div>
      }
    />
  );
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <input
        className="mt-1 w-full rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper outline-none focus:border-lime"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    </label>
  );
}

function Select({
  label,
  value,
  onChange,
  children,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  children: ReactNode;
}) {
  return (
    <label className="block text-xs text-muted">
      {label}
      <select
        className="mt-1 w-full rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
        value={value}
        onChange={(event) => onChange(event.target.value)}
      >
        {children}
      </select>
    </label>
  );
}
