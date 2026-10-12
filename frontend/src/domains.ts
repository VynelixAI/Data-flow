import type { Cadence, DomainId, Stage } from "./types";

export const DOMAINS: { id: DomainId; label: string; detail: string }[] = [
  { id: "stores", label: "Store operations", detail: "End-of-day sales, on-hand inventory, labor" },
  { id: "merchandising", label: "Merchandising", detail: "Price files, promotions, planograms" },
  { id: "supply", label: "Supply", detail: "Purchase orders, distribution, vendors" },
  { id: "customer", label: "Customer", detail: "Loyalty, tickets, returns" },
  { id: "finance", label: "Finance", detail: "Tender, shrink, margin" },
];

export const CADENCES: { id: Cadence; label: string }[] = [
  { id: "manual", label: "Manual" },
  { id: "after_close", label: "After close" },
  { id: "morning_publish", label: "Morning publish" },
  { id: "intraday", label: "Intraday" },
  { id: "weekly", label: "Weekly" },
];

export const STAGES: { id: Stage; label: string }[] = [
  { id: "dev", label: "Dev" },
  { id: "stage", label: "Stage" },
  { id: "prod", label: "Production" },
];

export function domainLabel(id: DomainId) {
  return DOMAINS.find((domain) => domain.id === id)?.label ?? "Unassigned";
}

export function cadenceLabel(id: Cadence) {
  return CADENCES.find((cadence) => cadence.id === id)?.label ?? id;
}

export function stageLabel(id: Stage) {
  return STAGES.find((stage) => stage.id === id)?.label ?? id;
}
