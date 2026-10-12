import { useState } from "react";
import CatalogSearch from "../components/CatalogSearch";

type TableRow = { name: string; full_name: string; table_type: string };

export default function CatalogView({
  onUse,
}: {
  onUse: (fullName: string, role: "source" | "destination") => void;
}) {
  const [catalog, setCatalog] = useState("main");
  const [schema, setSchema] = useState("dataflow");
  const [tables, setTables] = useState<TableRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  async function load() {
    setBusy(true);
    setError(null);
    try {
      const params = new URLSearchParams({ catalog: catalog.trim(), schema_name: schema.trim() });
      const response = await fetch(`/api/catalog/tables?${params.toString()}`);
      const body = await response.json();
      if (!response.ok) throw new Error(body.error || "Could not list tables");
      setTables(body.tables || []);
      setLoaded(true);
    } catch (reason) {
      setTables([]);
      setLoaded(true);
      setError(reason instanceof Error ? reason.message : "Could not list tables");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="h-full overflow-auto px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-4xl">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">Unity Catalog</p>
        <h1 className="mt-2 font-display text-3xl">Tables the pipelines are allowed to touch.</h1>
        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Search by a business name. The lookup covers tables and CSV/Excel files in volumes. It does not create
          anything.
        </p>
        <div className="mt-5">
          <CatalogSearch onPick={onUse} />
        </div>
        <form
          className="mt-6 flex flex-wrap items-end gap-3"
          onSubmit={(event) => {
            event.preventDefault();
            void load();
          }}
        >
          <label className="text-xs text-muted">
            Catalog
            <input
              className="mt-1 block rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
              value={catalog}
              onChange={(event) => setCatalog(event.target.value)}
            />
          </label>
          <label className="text-xs text-muted">
            Schema
            <input
              className="mt-1 block rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
              value={schema}
              onChange={(event) => setSchema(event.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={busy}
            className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >
            {busy ? "Loading…" : "List tables"}
          </button>
        </form>
        {error && <p className="mt-4 text-sm text-amber">{error}</p>}
        {loaded && !error && tables.length === 0 && (
          <p className="mt-4 text-sm text-muted">No tables in {catalog}.{schema}.</p>
        )}
        {tables.length > 0 && (
          <ul className="mt-5 divide-y divide-line rounded-2xl border border-line">
            {tables.map((table) => (
              <li key={table.full_name} className="flex items-center justify-between gap-3 px-4 py-3">
                <div>
                  <p className="font-mono text-sm">{table.full_name}</p>
                  <p className="text-xs text-muted">{table.table_type || "table"}</p>
                </div>
                <button
                  type="button"
                  onClick={() => onUse(table.full_name, "source")}
                  className="rounded-full border border-line px-3 py-1.5 text-xs"
                >
                  Use as source
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
