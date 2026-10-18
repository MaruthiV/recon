"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { StateBadge } from "@/components/StateBadge";
import { api, type CaseRow, money, NotSignedIn, when } from "@/lib/api";

export default function Inbox() {
  const router = useRouter();
  const [cases, setCases] = useState<CaseRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<CaseRow[]>("/v1/cases")
      .then(setCases)
      .catch((e) => (e instanceof NotSignedIn ? router.push("/login") : setError(String(e))));
  }, [router]);

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
    </>
  );
}
