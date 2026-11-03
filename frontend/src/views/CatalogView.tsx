
import { useState } from "react";
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
  // Start with empty fields. No previous values are restored.
  const [catalog, setCatalog] = useState("");
  const [schema, setSchema] = useState("");
  const [volume, setVolume] = useState("");

  const [tables, setTables] = useState<TableRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const [files, setFiles] = useState<VolumeFile[]>([]);
  const [filesError, setFilesError] = useState<string | null>(null);
  const [filesBusy, setFilesBusy] = useState(false);
  const [filesLoaded, setFilesLoaded] = useState(false);

  const [selectedSource, setSelectedSource] = useState<string | null>(null);
  const [selectedDestination, setSelectedDestination] = useState<string | null>(null);

  // List Unity Catalog tables.
  async function load() {
    if (!catalog.trim() || !schema.trim()) return;

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
    if (!catalog.trim() || !schema.trim() || !volume.trim()) return;

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

  // Handle source and destination selection.
  function handleUseFile(
    path: string,
    role: "source" | "destination"
  ) {
    if (role === "source") {
      setSelectedSource(path);
    } else {
      setSelectedDestination(path);
    }

    onUse(path, role);
  }

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

        {/* Catalog search */}
        <div className="mt-5">
          <CatalogSearch onPick={handleUseFile} />
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
              placeholder="Enter catalog name"
              onChange={(event) => {
                setCatalog(event.target.value);
                setLoaded(false);
                setTables([]);
                setFilesLoaded(false);
                setFiles([]);
              }}
            />
          </label>

          <label className="text-xs text-muted">
            Schema
            <input
              className="mt-1 block rounded-xl border border-line bg-ink px-3 py-2 text-sm text-paper"
              value={schema}
              placeholder="Enter schema name"
              onChange={(event) => {
                setSchema(event.target.value);
                setLoaded(false);
                setTables([]);
                setFilesLoaded(false);
                setFiles([]);
              }}
            />
          </label>

          <button
            type="submit"
            // disabled={busy || !catalog.trim() || !schema.trim()}
            disabled={busy}
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
            {tables.map((table) => {
              const isSource = selectedSource === table.full_name;
              const isDestination =
                selectedDestination === table.full_name;

              return (
                <li
                  key={table.full_name}
                  className="flex flex-wrap items-center justify-between gap-3 px-4 py-3"
                >
                  <div className="min-w-0 flex-1">
                    <p className="break-all font-mono text-sm">
                      {table.full_name}
                    </p>
                    <p className="text-xs text-muted">
                      {table.table_type || "table"}
                    </p>
                  </div>

                  <div className="flex flex-wrap gap-2">
                    <button
                      type="button"
                      aria-pressed={isSource}
                      onClick={() =>
                        handleUseFile(table.full_name, "source")
                      }
                      className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                        isSource
                          ? "border-green-600 bg-green-600 text-white"
                          : "border-line hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                      }`}
                    >
                      {isSource ? "✓ Source Selected" : "Use as source"}
                    </button>

                    <button
                      type="button"
                      aria-pressed={isDestination}
                      onClick={() =>
                        handleUseFile(table.full_name, "destination")
                      }
                      className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                        isDestination
                          ? "border-green-600 bg-green-600 text-white"
                          : "border-line hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                      }`}
                    >
                      {isDestination
                        ? "✓ Destination Selected"
                        : "Use as destination"}
                    </button>
                  </div>
                </li>
              );
            })}
          </ul>
        )}

        {/* Volume files browser */}
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
                placeholder="Enter volume name"
                onChange={(event) => {
                  setVolume(event.target.value);
                  setFilesLoaded(false);
                  setFiles([]);
                }}
                required
              />
            </label>

            <button
              type="submit"
              // disabled={
              //   filesBusy ||
              //   !catalog.trim() ||
              //   !schema.trim() ||
              //   !volume.trim()
              // }
              disabled={filesBusy}
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
                      aria-pressed={selectedSource === file.path}
                      onClick={() =>
                        handleUseFile(file.path, "source")
                      }
                      className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                        selectedSource === file.path
                          ? "border-green-600 bg-green-600 text-white"
                          : "border-line hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                      }`}
                    >
                      {selectedSource === file.path
                        ? "✓ Source Selected"
                        : "Use as source"}
                    </button>

                    <button
                      type="button"
                      aria-pressed={selectedDestination === file.path}
                      onClick={() =>
                        handleUseFile(file.path, "destination")
                      }
                      className={`rounded-full border px-3 py-2 text-xs font-semibold transition-colors ${
                        selectedDestination === file.path
                          ? "border-green-600 bg-green-600 text-white"
                          : "border-line hover:border-green-500 hover:bg-green-50 hover:text-green-800"
                      }`}
                    >
                      {selectedDestination === file.path
                        ? "✓ Destination Selected"
                        : "Use as destination"}
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
