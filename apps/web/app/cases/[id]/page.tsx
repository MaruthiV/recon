"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { StateBadge } from "@/components/StateBadge";
import { Timeline } from "@/components/Timeline";
import { api, type CaseDetail, micros, money, NotSignedIn, when } from "@/lib/api";

type Me = { user: string; tenant_id: string; dev: boolean; faults: boolean };

export default function Workspace() {
  const { id } = useParams<{ id: string }>();
  const router = useRouter();
  const [d, setD] = useState<CaseDetail | null>(null);
  const [me, setMe] = useState<Me | null>(null);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);

  const load = useCallback(() => {
    api<CaseDetail>(`/v1/cases/${id}`)
      .then(setD)
      .catch((e) => (e instanceof NotSignedIn ? router.push("/login") : setNote(String(e))));
  }, [id, router]);

  useEffect(() => {
    load();
    api<Me>("/v1/me").then(setMe).catch(() => undefined);
  }, [load]);

  // every timeline event may change the case, refetch once things settle
  const onEvent = useCallback(() => {
    if (pending.current) clearTimeout(pending.current);
    pending.current = setTimeout(load, 250);
  }, [load]);

  async function start(mode: "rules" | "agent") {
    setBusy(true);
    try {
      await api(`/v1/cases/${id}/investigations`, { method: "POST", body: JSON.stringify({ mode }) });
      setNote(`Started a ${mode} investigation.`);
      load();
    } catch (e) {
      setNote(String(e));
    } finally {
      setBusy(false);
    }
  }

  async function arm() {
    await api("/internal/demo/faults", { method: "POST", body: JSON.stringify({ point: "FP4" }) });
    setNote("Armed: the next worker to send a refund dies right after the provider commits it.");
  }

  if (!d) return <p className="muted">Loading…</p>;
  const { case: c, plan, run } = d;
  const paymentRef = new Map(d.payments.map((p, i) => [p.id, `payment ${i + 1}`]));

  return (
    <>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          <h1>{c.subject}</h1>
          <span className="muted">
            <code>{c.id}</code> · updated {when(c.updated_at)}
          </span>
        </div>
        <StateBadge state={c.state} resolution={c.resolution} />
      </div>

      <div className="panel" style={{ marginTop: 16 }}>
        <div className="row">
          {c.state === "OPEN" ? (
            <>
              <button className="primary" disabled={busy} onClick={() => start("rules")}>
                Investigate (rules, no model)
              </button>
              <button disabled={busy} onClick={() => start("agent")}>
                Investigate (agents)
              </button>
            </>
          ) : (
            <span className="muted">Investigation started, follow it in the timeline.</span>
          )}
          {me?.dev && me.faults && (
            <button className="danger" onClick={arm}>
              Kill the worker after the provider commits (FP4)
            </button>
          )}
        </div>
        {note && <p className="muted">{note}</p>}
      </div>

      <div className="grid">
        <div>
          <div className="panel">
            <h2>Customer request</h2>
            {d.messages.map((m) => (
              <p key={m.created_at} className="quote">
                {m.body}
              </p>
            ))}
          </div>

          <div className="panel">
            <h2>Order and payments</h2>
            {d.order && (
              <p className="muted">
                Order <code>{d.order.id}</code> · {money(d.order.total_minor, d.order.currency)} · placed{" "}
                {when(d.order.placed_at)}
              </p>
            )}
            <table>
              <thead>
                <tr>
                  <th>Payment</th>
                  <th>Captured</th>
                  <th>Amount</th>
                  <th>Refunded</th>
                  <th>Held</th>
                </tr>
              </thead>
              <tbody>
                {d.payments.map((p) => (
                  <tr key={p.id}>
                    <td>
                      {paymentRef.get(p.id)} <div className="muted"><code>{p.id}</code></div>
                    </td>
                    <td className="muted">{when(p.captured_at)}</td>
                    <td>{money(p.amount_minor, p.currency)}</td>
                    <td>{money(p.refunded_minor, p.currency)}</td>
                    <td>{money(p.reserved_minor, p.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <h2>Plan</h2>
            {!plan && <p className="muted">No plan yet.</p>}
            {plan && (
              <>
                <p>
                  <strong>{plan.payload.recommended_disposition}</strong>{" "}
                  {plan.payload.amount_minor !== null && money(plan.payload.amount_minor, plan.payload.currency ?? "usd")}
                  {plan.payload.payment_id && <> on {paymentRef.get(plan.payload.payment_id) ?? plan.payload.payment_id}</>} ·{" "}
                  <span className="muted">
                    {plan.payload.reason_code}, proposed by {plan.source}
                  </span>
                </p>
                <p className="quote">{plan.payload.explanation}</p>
                <p>
                  Validator: <StateBadge state={plan.decision} />{" "}
                  <span className="muted">{plan.verdict.reasons.join("; ")}</span>
                </p>
                <p className="muted">Evidence cited</p>
                <ul>
                  {plan.payload.evidence_refs.map((r) => (
                    <li key={r}>
                      <code>{r}</code>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>

          <div className="panel">
            <h2>Refunds</h2>
            {d.commands.length === 0 && <p className="muted">No refund prepared.</p>}
            {d.commands.length > 0 && (
              <table>
                <thead>
                  <tr>
                    <th>Command</th>
                    <th>State</th>
                    <th>Amount</th>
                    <th>Provider refund</th>
                    <th>Sends</th>
                  </tr>
                </thead>
                <tbody>
                  {d.commands.map((cmd) => (
                    <tr key={cmd.id}>
                      <td>
                        <code>{cmd.id}</code>
                      </td>
                      <td>
                        <StateBadge state={cmd.state} />
                      </td>
                      <td>{money(cmd.amount_minor, cmd.currency)}</td>
                      <td>{cmd.provider_refund_id ? <code>{cmd.provider_refund_id}</code> : "–"}</td>
                      <td>{cmd.attempts}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        </div>

        <div>
          <Timeline caseId={id} onEvent={onEvent} />
          {run && (
            <div className="panel">
              <h2>Run</h2>
              <p className="muted">
                <code>{run.id}</code> · stage {run.stage} · investigation {run.investigation}
                <br />
                generation {run.generation}
                {run.lease_owner ? ` · leased by ${run.lease_owner}` : ""}
                <br />
                {run.model_calls} model calls · {run.tool_calls} tool calls · est. {micros(run.est_cost_micros)} at list price
              </p>
              {run.failure && <p className="error">{run.failure}</p>}
              {run.evidence && (
                <details>
                  <summary className="muted">Agent evidence</summary>
                  <pre>{JSON.stringify(run.evidence, null, 2)}</pre>
                </details>
              )}
            </div>
          )}
        </div>
      </div>
    </>
  );
}
