export type CaseRow = {
  id: string;
  subject: string;
  state: string;
  resolution: string | null;
  updated_at: string;
  proposed_amount_minor: number | null;
  needs_approval: boolean;
};

export type DeadLetter = {
  id: number;
  case_id: string;
  run_id: string;
  stage: string;
  attempts: number;
  last_error: string | null;
  dead_at: string;
};

export type Payment = {
  id: string;
  provider_payment_id: string;
  amount_minor: number;
  currency: string;
  status: string;
  captured_at: string;
  refunded_minor: number;
  reserved_minor: number;
};

export type Command = {
  id: string;
  state: string;
  amount_minor: number;
  currency: string;
  payment_id: string;
  provider_refund_id: string | null;
  attempts: number;
  idempotency_key: string;
};

export type Plan = {
  id: string;
  source: string;
  decision: string;
  material_hash: string;
  payload: {
    recommended_disposition: string;
    payment_id: string | null;
    amount_minor: number | null;
    currency: string | null;
    reason_code: string;
    explanation: string;
    evidence_refs: string[];
    policy_version_ids: string[];
    missing_evidence: string[];
  };
  verdict: { decision: string; reasons: string[]; entitlement_ref: string | null };
  created_at: string;
};

export type Run = {
  id: string;
  stage: string;
  investigation: string;
  workflow_version: string;
  models: Record<string, string>;
  lease_owner: string | null;
  generation: number;
  model_calls: number;
  tool_calls: number;
  est_cost_micros: number;
  evidence: Record<string, unknown> | null;
  failure: string | null;
};

export type CaseDetail = {
  case: { id: string; subject: string; state: string; resolution: string | null; version: number; updated_at: string };
  messages: { author: string; body: string; created_at: string }[];
  order: { id: string; total_minor: number; currency: string; placed_at: string } | null;
  payments: Payment[];
  commands: Command[];
  plan: Plan | null;
  run: Run | null;
};

export type TimelineEvent = {
  id: number;
  kind: string;
  label: string;
  engineering: boolean;
  actor: string;
  at: string;
  command_id: string | null;
  data: Record<string, unknown>;
};

export class NotSignedIn extends Error {}

export async function api<T>(path: string, init?: RequestInit): Promise<T> {
  // one key per write, so a resent request gets the first answer instead of doing it twice
  const key: Record<string, string> = init?.method === "POST" ? { "Idempotency-Key": crypto.randomUUID() } : {};
  const r = await fetch(`/api${path}`, {
    ...init,
    headers: { "content-type": "application/json", ...key, ...(init?.headers ?? {}) },
    cache: "no-store",
  });
  if (r.status === 401) throw new NotSignedIn();
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json() as Promise<T>;
}

// integer minor units in, a display string out, no floats on the way
export function money(minor: number | null | undefined, currency = "usd"): string {
  if (minor === null || minor === undefined) return "–";
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  return `${sign}${Math.trunc(abs / 100)}.${String(abs % 100).padStart(2, "0")} ${currency.toUpperCase()}`;
}

export function when(iso: string): string {
  return new Date(iso).toLocaleString();
}

// micro-dollars to "$0.0131" with integers only
export function micros(m: number): string {
  return `$${Math.trunc(m / 1_000_000)}.${String(m % 1_000_000).padStart(6, "0").slice(0, 4)}`;
}
