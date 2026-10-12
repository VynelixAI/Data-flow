import { useState } from "react";
import type { UploadedTable } from "../types";

export default function FileUpload({
  onUploaded,
  disabled,
}: {
  onUploaded: (table: UploadedTable) => void;
  disabled?: boolean;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function send(file: File) {
    setBusy(true);
    setError(null);
    const body = new FormData();
    body.append("file", file);
    try {
      const response = await fetch("/api/upload", { method: "POST", body });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(payload.error || "Upload failed");
      }
      onUploaded(payload as UploadedTable);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Upload failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div>
      <label
        className={`inline-flex cursor-pointer items-center whitespace-nowrap rounded-full border border-line px-3 py-2 text-xs text-paper hover:border-lime ${
          disabled || busy ? "pointer-events-none opacity-50" : ""
        }`}
      >
        {busy ? "Uploading…" : "Upload CSV / Excel"}
        <input
          className="hidden"
          type="file"
          accept=".csv,.txt,.tsv,.xlsx,.xlsm,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
          onChange={(event) => {
            const file = event.target.files?.[0];
            if (file) void send(file);
            event.target.value = "";
          }}
        />
      </label>
      {error && <p className="mt-2 text-xs text-coral">{error}</p>}
    </div>
  );
}
