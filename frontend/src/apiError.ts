export function apiError(body: unknown, fallback: string): string {
  if (!body || typeof body !== "object") return fallback;
  const record = body as Record<string, unknown>;
  if (typeof record.error === "string" && record.error.trim()) return record.error;
  if (typeof record.detail === "string" && record.detail.trim()) return record.detail;
  if (Array.isArray(record.detail)) {
    const first = record.detail[0] as { msg?: string; loc?: unknown[] } | undefined;
    if (first?.msg) {
      const loc = Array.isArray(first.loc) ? first.loc.filter((part) => part !== "body").join(".") : "";
      return loc ? `${loc}: ${first.msg}` : first.msg;
    }
  }
  return fallback;
}
