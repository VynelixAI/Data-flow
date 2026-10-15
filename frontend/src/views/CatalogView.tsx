
import { useEffect, useState } from "react";
import { apiError } from "../apiError";
import CatalogSearch from "../components/CatalogSearch";

type TableRow = {
  name: string;
  full_name: string;
  table_type: string;
};

type VolumeFile = {
  name: string;
  path: string;
  is_directory: boolean;
  size: number | null;
};

export default function CatalogView({
  onUse,
}: {
  onUse: (fullName: string, role: "source" | "destination") => void;
}) {
  
const [catalog, setCatalog] = useState(() => {
  try {
    return localStorage.getItem("dataflow-catalog") ?? "main";
  } catch {
    return "main";
  }
});

const [schema, setSchema] = useState(() => {
  try {
    return localStorage.getItem("dataflow-schema") ?? "dataflow";
  } catch {
    return "dataflow";
  }
});

  const [tables, setTables] = useState<TableRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  
const [volume, setVolume] = useState(() => {
  try {
    return localStorage.getItem("dataflow-volume") ?? "dummy_data";
  } catch {
    return "dummy_data";
  }
});

  const [files, setFiles] = useState<VolumeFile[]>([]);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [filesBusy, setFilesBusy] = useState(false);
  const [filesLoaded, setFilesLoaded] = useState(false);
  
useEffect(() => {
  try {
    localStorage.setItem("dataflow-catalog", catalog);
    localStorage.setItem("dataflow-schema", schema);
    localStorage.setItem("dataflow-volume", volume);
  } catch {
    // Keep Catalog usable if storage is unavailable.
  }
}, [catalog, schema, volume]);

  // Load available catalogs.
  useEffect(() => {
    let cancelled = false;

    void (async () => {
      try {
        const response = await fetch("/api/catalogs");
        const body = await response.json();

        if (!response.ok || cancelled) return;

        const names = (body.catalogs || []) as string[];

        setCatalog((current) => {
          if (names.includes(current)) return current;

          return (
            names.find((name) => name === "workspace") ||
            names.find(
              (name) =>
                !["system", "samples", "hive_metastore"].includes(name)
            ) ||
            current
          );
        });
      } catch {
        // Keep the current catalog if discovery fails.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, []);

  // Load schemas when the catalog changes.
  useEffect(() => {
    const name = catalog.trim();
    if (!name) return;

    let cancelled = false;

    void (async () => {
      try {
        const response = await fetch(
          `/api/catalog/schemas?catalog=${encodeURIComponent(name)}`
        );
        const body = await response.json();

        if (!response.ok || cancelled) return;

        const names = (body.schemas || []) as string[];

        setSchema((current) =>
          names.includes(current)
            ? current
            : names.includes("default")
              ? "default"
              : names[0] || current
        );
      } catch {
        // Keep the current schema if discovery fails.
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [catalog]);

  // List Unity Catalog tables.
  async function load() {
    setBusy(true);
    setError(null);

    try {
      const params = new URLSearchParams({
        catalog: catalog.trim(),
        schema_name: schema.trim(),
      });

      const response = await fetch(
        `/api/catalog/tables?${params.toString()}`
      );
      const body = await response.json();

      if (!response.ok) {
        throw new Error(apiError(body, "Could not list tables"));
      }

      setTables(body.tables || []);
      setLoaded(true);
    } catch (reason) {
      setTables([]);
      setLoaded(true);
      setError(
        reason instanceof Error
          ? reason.message
          : "Could not list tables"
      );
    } finally {
      setBusy(false);
    }
  }

  // List files from an existing Unity Catalog volume.
  async function loadVolumeFiles() {
    setFilesBusy(true);
    setFilesError(null);

    try {
      const params = new URLSearchParams({
        catalog: catalog.trim(),
        schema_name: schema.trim(),
        volume: volume.trim(),
      });

      const response = await fetch(
        `/api/catalog/volume-files?${params.toString()}`
      );
      const body = await response.json();

      if (!response.ok) {
        throw new Error(
          apiError(body, "Could not list volume files")
        );
      }

      setFiles(body.files || []);
      setFilesLoaded(true);
    } catch (reason) {
      setFiles([]);
      setFilesLoaded(true);
      setFilesError(
        reason instanceof Error
          ? reason.message
          : "Could not list volume files"
      );
    } finally {
      setFilesBusy(false);
    }
  }

  const regularFiles = files.filter((file) => !file.is_directory);

  return (
    <div className="h-full overflow-auto px-4 py-6 sm:px-8">
      <div className="mx-auto max-w-4xl">
        <p className="text-xs uppercase tracking-[0.16em] text-muted">
          Unity Catalog
        </p>

        <h1 className="mt-2 font-display text-3xl">
          Tables the pipelines are allowed to touch.
        </h1>

        <p className="mt-2 max-w-2xl text-sm leading-6 text-muted">
          Search by a business name. The lookup covers tables and CSV/Excel
          files in volumes. It does not create anything.
        </p>

        {/* Existing catalog search */}
        <div className="mt-5">
          <CatalogSearch onPick={onUse} />
        </div>

        {/* Catalog and schema selection */}
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
              onChange={(event) => {
                setCatalog(event.target.value);
                setLoaded(false);
                setFilesLoaded(false);
              }}
            />
          </label>

          <label className="text-xs text-muted">
            Schema
            <input
              className="mt-1 block rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
              value={schema}
              onChange={(event) => {
                setSchema(event.target.value);
                setLoaded(false);
                setFilesLoaded(false);
              }}
            />
          </label>

          <button
            type="submit"
            disabled={busy || !catalog.trim() || !schema.trim()}
            className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
          >
            {busy ? "Loading…" : "List tables"}
          </button>
        </form>

        {/* Table results */}
        {error && (
          <p className="mt-4 text-sm text-amber">{error}</p>
        )}

        {loaded && !error && tables.length === 0 && (
          <p className="mt-4 text-sm text-muted">
            No tables in {catalog}.{schema}.
          </p>
        )}

        {tables.length > 0 && (
          <ul className="mt-5 divide-y divide-line rounded-2xl border border-line">
            {tables.map((table) => (
              <li
                key={table.full_name}
                className="flex items-center justify-between gap-3 px-4 py-3"
              >
                <div className="min-w-0">
                  <p className="break-all font-mono text-sm">
                    {table.full_name}
                  </p>
                  <p className="text-xs text-muted">
                    {table.table_type || "table"}
                  </p>
                </div>

                <button
                  type="button"
                  onClick={() => onUse(table.full_name, "source")}
                  className="shrink-0 rounded-full border border-line px-3 py-1.5 text-xs"
                >
                  Use as source
                </button>
              </li>
            ))}
          </ul>
        )}

        {/* Volume files browser: separate from table results */}
        <section className="mt-8 border-t border-line pt-6">
          <h2 className="text-xl font-semibold">Volume files</h2>

          <p className="mt-2 text-sm text-muted">
            Browse CSV and Excel files stored in an existing Unity Catalog
            volume.
          </p>

          <form
            className="mt-4 flex flex-wrap items-end gap-3"
            onSubmit={(event) => {
              event.preventDefault();
              void loadVolumeFiles();
            }}
          >
            <label className="text-xs text-muted">
              Volume name
              <input
                className="mt-1 block rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
                value={volume}
                onChange={(event) => {
                  setVolume(event.target.value);
                  setFilesLoaded(false);
                }}
                required
              />
            </label>

            <button
              type="submit"
              disabled={
                filesBusy ||
                !catalog.trim() ||
                !schema.trim() ||
                !volume.trim()
              }
              className="rounded-full bg-lime px-4 py-2 text-sm font-semibold text-white disabled:opacity-40"
            >
              {filesBusy ? "Loading…" : "List volume files"}
            </button>
          </form>

          {filesError && (
            <p className="mt-4 text-sm text-amber">{filesError}</p>
          )}

          {filesLoaded && !filesError && regularFiles.length === 0 && (
            <p className="mt-4 text-sm text-muted">
              No files found in {catalog}.{schema}.{volume}.
            </p>
          )}

          {regularFiles.length > 0 && (
            <ul className="mt-5 divide-y divide-line rounded-2xl border border-line">
              {regularFiles.map((file) => (
                <li
                  key={file.path}
                  className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-all font-mono text-sm">
                      {file.name}
                    </p>

                    <p className="break-all text-xs text-muted">
                      {file.path}
                    </p>

                    <p className="text-xs text-muted">
                      {file.size == null
                        ? "Size unavailable"
                        : `${(file.size / 1024).toFixed(2)} KB`}
                    </p>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      onClick={() => onUse(file.path, "source")}
                      className="rounded-full border border-line px-3 py-1.5 text-xs"
                    >
                      Use as source
                    </button>

                    <button
                      type="button"
                      onClick={() => onUse(file.path, "destination")}
                      className="rounded-full border border-line px-3 py-1.5 text-xs"
                    >
                      Use as destination
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </section>
      </div>
    </div>
  );
}
