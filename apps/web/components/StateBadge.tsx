const TONE: Record<string, string> = {
  CLOSED: "ok",
  CONFIRMED_SUCCESS: "ok",
  ESCALATED: "bad",
  CONFIRMED_FAILURE: "bad",
  MANUAL_REVIEW: "bad",
  AWAITING_APPROVAL: "warn",
  AWAITING_INFO: "warn",
  RECONCILING: "warn",
  UNKNOWN: "warn",
  SUBMITTING: "warn",
  PENDING_PROVIDER: "warn",
};

export function StateBadge({ state, resolution }: { state: string; resolution?: string | null }) {
  return <span className={`badge ${TONE[state] ?? ""}`}>{resolution ? `${state} · ${resolution}` : state}</span>;
}
