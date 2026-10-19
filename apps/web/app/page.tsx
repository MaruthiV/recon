"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { StateBadge } from "@/components/StateBadge";
import { api, type CaseRow, type DeadLetter, money, NotSignedIn, when } from "@/lib/api";

export default function Inbox() {
  const router = useRouter();
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const [dead, setDead] = useState<DeadLetter[]>([]);

  const loadDead = useCallback(() => {
    // supervisors only, everyone else gets a 403 and simply sees nothing
    api<DeadLetter[]>("/v1/ops/dead-letters")
      .then(setDead)
      .catch(() => setDead([]));
  }, []);

  useEffect(() => {
    api<CaseRow[]>("/v1/cases")
      .then(setCases)
      .catch((e) => (e instanceof NotSignedIn ? router.push("/login") : setError(String(e))));
    loadDead();
  }, [router, loadDead]);

  async function requeue(id: number) {
    try {
      await api(`/v1/ops/dead-letters/${id}/requeue`, { method: "POST", body: "{}" });
      loadDead();
    } catch (e) {
      setError(String(e));
    }
  }

  return (
    <>
      <h1>Cases</h1>
      <p className="muted">Customer payment problems for your tenant.</p>
      {error && <p className="error">{error}</p>}
      <div className="panel">
        <table>
          <thead>
            <tr>
              <th>Case</th>
              <th>Status</th>
              <th>Proposed</th>
              <th>Approval</th>
              <th>Updated</th>
            </tr>
          </thead>
          <tbody>
            {cases?.map((c) => (
              <tr key={c.id}>
                <td>
                  <a href={`/cases/${c.id}`}>{c.subject}</a>
                  <div className="muted">
                    <code>{c.id}</code>
                  </div>
                </td>
                <td>
                  <StateBadge state={c.state} resolution={c.resolution} />
                </td>
                <td>{money(c.proposed_amount_minor)}</td>
                <td>{c.needs_approval ? <span className="badge warn">needs approval</span> : "–"}</td>
                <td className="muted">{when(c.updated_at)}</td>
              </tr>
            ))}
            {cases?.length === 0 && (
              <tr>
                <td colSpan={5} className="muted">
                  No cases yet. Run <code>recon seed --profile demo</code> or <code>make dev</code>.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      {dead.length > 0 && (
        <div className="panel">
          <h2>Stuck work</h2>
          <p className="muted">Stages the dispatcher gave up on. Fix the cause, then send them round again.</p>
          <table>
            <thead>
              <tr>
                <th>Case</th>
                <th>Stage</th>
                <th>Tries</th>
                <th>Last error</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {dead.map((d) => (
                <tr key={d.id}>
                  <td>
                    <a href={`/cases/${d.case_id}`}>
                      <code>{d.case_id}</code>
                    </a>
                  </td>
                  <td>{d.stage}</td>
                  <td className="num">{d.attempts}</td>
                  <td>
                    <code>{d.last_error}</code>
                  </td>
                  <td>
                    <button onClick={() => requeue(d.id)}>Requeue</button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </>
  );
}
