
import { useState } from "react";

type Hit = {
  kind: string;
  name: string;
  rank: number;
  table_type?: string;
};

export default function CatalogSearch({
  onPick,
}: {
  onPick: (name: string, role: "source" | "destination") => void;
}) {
  const [query, setQuery] = useState("");
  const [hits, setHits] = useState<Hit[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);

  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [selectedDestination, setSelectedDestination] = useState<string | null>(null);

  async function search() {
    const text = query.trim();

    if (text.length < 2) {
      setError("Type at least 2 characters.");
      return;
    }

    setBusy(true);
    setError(null);
    setNote(null);

    try {
      const response = await fetch(
        `/api/catalog/search?q=${encodeURIComponent(text)}`
      );

      const body = await response.json();

      if (!response.ok) {
        throw new Error(body.error || "Search failed");
      }

      setHits(body.matches || []);
      setNote(
        body.truncated
          ? "Showing the closest matches. Narrow the name if yours is missing."
          : null
      );
    } catch (reason) {
      setHits([]);
      setError(
        reason instanceof Error ? reason.message : "Search failed"
      );
    } finally {
      setBusy(false);
    }
  }

  function selectFile(name: string, role: "source" | "destination") {
    onPick(name, role);

    if (role === "source") {
      setSelectedSource(name);
    } else {
      setSelectedDestination(name);
    }
  }

  return (
    <div>
      <form
        className="flex flex-wrap items-end gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void search();
        }}
      >
        <label className="min-w-[220px] flex-1 text-xs text-muted">
          Find a table or file

          <input
            className="mt-1 block w-full rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper outline-none focus:border-lime"
            value={query}
            placeholder="orders, store inventory, prices.csv"
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>

        <button
          type="submit"
          disabled={busy}
          className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
        >
          {busy ? "Searching…" : "Search catalog"}
        </button>
      </form>

      {error && (
        <p className="mt-3 text-sm text-amber">{error}</p>
      )}

      {note && (
        <p className="mt-3 text-xs text-muted">{note}</p>
      )}

      {hits.length > 0 && (
        <ul className="mt-4 divide-y divide-line rounded-2xl border border-line">
          {hits.map((hit) => {
            const isSource = selectedSource === hit.name;
            const isDestination = selectedDestination === hit.name;

            return (
              <li
                key={`${hit.kind}-${hit.name}`}
                className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
              >
                <div className="min-w-0 flex-1">
                  <p className="break-all font-mono text-sm">
                    {hit.name}
                  </p>

                  <p className="text-xs text-muted">
                    {hit.kind === "volume_file" ? "Volume file" : "Table"}
                  </p>

                  {(isSource || isDestination) && (
                    <div className="mt-2 flex flex-wrap gap-2">
                      {isSource && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2.5 py-1 text-xs font-semibold text-green-800">
                          ✓ Selected as Source
                        </span>
                      )}

                      {isDestination && (
                        <span className="inline-flex items-center gap-1 rounded-full bg-green-100 px-2.5 py-1 text-xs font-semibold text-green-800">
                          ✓ Selected as Destination
                        </span>
                      )}
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap gap-2">
                  <button
                    type="button"
                    aria-pressed={isSource}
                    onClick={() => selectFile(hit.name, "source")}
                    className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                      isSource
                        ? "border-green-600 bg-green-600 text-white"
                        : "border-line bg-panel text-paper hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                    }`}
                  >
                    {isSource ? "✓ Source selected" : "Use as source"}
                  </button>

                  <button
                    type="button"
                    aria-pressed={isDestination}
                    onClick={() => selectFile(hit.name, "destination")}
                    className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                      isDestination
                        ? "border-green-600 bg-green-600 text-white"
                        : "border-line bg-panel text-paper hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                    }`}
                  >
                    {isDestination
                      ? "✓ Destination selected"
                      : "Use as destination"}
                  </button>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
