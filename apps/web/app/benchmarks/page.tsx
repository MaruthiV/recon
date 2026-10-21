"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { api, type BenchmarkReport, type BenchmarkRow, micros, NotSignedIn } from "@/lib/api";

const ARM_NAMES: Record<string, string> = { rules: "rules", graph: "multi-agent", single: "single agent" };
const ARM_ORDER = ["rules", "graph", "single"];

// failed verifier checks come back as ids like "3_amount"
function why(w: unknown) {
  if (typeof w === "string") return w.slice(0, 90);
  if (Array.isArray(w)) return `failed: ${w.map((c) => String(c).replace(/^\d+_/, "").replaceAll("_", " ")).join(", ")}`;
  return JSON.stringify(w);
}

function label(r: { arm: string; model: string | null }) {
  const name = ARM_NAMES[r.arm] ?? r.arm;
  return r.model ? `${name} · ${r.model.replace("ollama:", "")}` : name;
}

function pct(x: number) {
  return `${Math.round(x * 100)}%`;
}

export default function Benchmarks() {
  const router = useRouter();
  const [rows, setRows] = useState<BenchmarkRow[] | null>(null);
  const [reports, setReports] = useState<Record<string, BenchmarkReport>>({});
  const [open, setOpen] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<BenchmarkRow[]>("/v1/benchmarks")
      .then(setRows)
      .catch((e) => (e instanceof NotSignedIn ? router.push("/login") : setError(String(e))));
  }, [router]);

  // the newest run of each arm on the newest validation split, so stale runs don't sneak into the comparison,
  // plus every pass of that arm on the same split for the repeat rows
  const { shown, passes } = useMemo(() => {
    const passes: Record<string, BenchmarkRow[]> = {};
    if (!rows?.length) return { shown: [], passes };
    const val = rows.filter((r) => r.split === "val");
    const pool = val.length ? val : rows;
    const split = pool[0].split_sha256;
    // a run filtered to a few families isn't comparable to a full one
    const full = Math.max(...pool.filter((r) => r.split_sha256 === split).map((r) => r.n));
    const shown = pool
      .filter((r) => {
        if (r.split_sha256 !== split || r.n !== full) return false;
        const key = `${r.arm}|${r.model}`;
        passes[key] = [...(passes[key] ?? []), r];
        return passes[key].length === 1;
      })
      .sort((a, b) => ARM_ORDER.indexOf(a.arm) - ARM_ORDER.indexOf(b.arm));
    return { shown, passes };
  }, [rows]);
  const group = (r: BenchmarkRow) => passes[`${r.arm}|${r.model}`] ?? [r];
  const repeated = shown.some((r) => group(r).length > 1);

  // a case counts only if no pass missed it
  function everyPass(runs: BenchmarkRow[]) {
    const reps = runs.map((p) => reports[p.id]);
    if (reps.some((x) => !x)) return "…";
    const missed = new Set(reps.flatMap((x) => x.failures.map((f) => f.seed)));
    return `${runs[0].n - missed.size}/${runs[0].n}`;
  }

  useEffect(() => {
    for (const r of Object.values(passes).flat()) {
      if (reports[r.id]) continue;
      api<{ report: BenchmarkReport }>(`/v1/benchmarks/${r.id}`)
        .then((d) => setReports((all) => ({ ...all, [r.id]: d.report })))
        .catch((e) => setError(String(e)));
    }
  }, [passes, reports]);

  const families = useMemo(
    () => [...new Set(Object.values(reports).flatMap((r) => Object.keys(r.per_family)))].sort(),
    [reports],
  );

  return (
    <>
      <h1>Benchmarks</h1>
      <p className="muted">
        Every approach on the same frozen split. A case passes only if the independent verifier agrees with the final
        state at the provider and in recon.
      </p>
      {error && <p className="error">{error}</p>}
      {rows && rows.length === 0 && (
        <div className="panel">
          <p className="muted">
            No finished runs yet. Try <code>uv run recon eval --split val --arm rules</code>.
          </p>
        </div>
      )}
      {shown.length > 0 && (
        <div className="panel">
          <h2>
            {shown[0].split} split · {shown[0].n} cases · <code>{shown[0].split_sha256.slice(0, 12)}</code>
          </h2>
          {repeated && <p className="muted">The first two rows count every pass on this split. Everything else on the page is the newest pass.</p>}
          <div className="scroll">
            <table className="compare">
              <thead>
                <tr>
                  <th />
                  {shown.map((r) => (
                    <th key={r.id}>{label(r)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {repeated && (
                  <>
                    <tr>
                      <td className="muted">right every pass</td>
                      {shown.map((r) => (
                        <td key={r.id} className="num">
                          {everyPass(group(r))}
                        </td>
                      ))}
                    </tr>
                    <tr>
                      <td className="muted">right, pass by pass</td>
                      {shown.map((r) => (
                        <td key={r.id} className="num">
                          {group(r)
                            .map((p) => p.passed)
                            .reverse()
                            .join(", ")}
                        </td>
                      ))}
                    </tr>
                  </>
                )}
                {(
                  [
                    ["right outcome", (b: BenchmarkReport) => `${b.passed}/${b.n} (${pct(b.task_success)})`],
                    ["95% interval", (b: BenchmarkReport) => `${pct(b.task_success_ci95[0])} to ${pct(b.task_success_ci95[1])}`],
                    ["wrong answer, closed", (b: BenchmarkReport) => `${b.wrong_closes}`],
                    ["handed to a person", (b: BenchmarkReport) => `${b.escalated}`],
                    ["money moved correctly", (b: BenchmarkReport) => `${b.money_safe}/${b.n}`],
                    ["no person needed", (b: BenchmarkReport) => `${b.autonomous_resolution}/${b.n}`],
                    ["seconds per case (p50 / p95)", (b: BenchmarkReport) => `${b.seconds_p50} / ${b.seconds_p95}`],
                    ["model cost at list price", (b: BenchmarkReport) => micros(b.model_cost_micros)],
                  ] as const
                ).map(([name, cell]) => (
                  <tr key={name}>
                    <td className="muted">{name}</td>
                    {shown.map((r) => (
                      <td key={r.id} className="num">
                        {reports[r.id] ? cell(reports[r.id]) : "…"}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {families.length > 0 && (
        <div className="panel">
          <h2>By case family</h2>
          <div className="scroll">
            <table className="compare">
              <thead>
                <tr>
                  <th />
                  {shown.map((r) => (
                    <th key={r.id}>{label(r)}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {families.map((f) => (
                  <tr key={f}>
                    <td>{f.replaceAll("_", " ")}</td>
                    {shown.map((r) => {
                      const c = reports[r.id]?.per_family[f];
                      const tone = !c ? "" : c.passed === c.n ? "ok" : c.passed * 2 >= c.n ? "warn" : "bad";
                      return (
                        <td key={r.id}>{c ? <span className={`badge ${tone}`}>{`${c.passed}/${c.n}`}</span> : "–"}</td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
      {shown.map((r) =>
        reports[r.id] && reports[r.id].failures.length > 0 ? (
          <div className="panel" key={r.id}>
            <h2>
              <button className="link" onClick={() => setOpen(open === r.id ? null : r.id)}>
                {open === r.id ? "▾" : "▸"} {label(r)}: {reports[r.id].failures.length} misses
              </button>
            </h2>
            {open === r.id && (
              <table>
                <thead>
                  <tr>
                    <th>Case</th>
                    <th>Ended</th>
                    <th>Plan</th>
                    <th>Why</th>
                  </tr>
                </thead>
                <tbody>
                  {reports[r.id].failures.map((f) => (
                    <tr key={f.seed}>
                      <td>
                        {f.family.replaceAll("_", " ")} / {f.variant.replaceAll("_", " ")}
                      </td>
                      <td>{f.observed}</td>
                      <td>{f.disposition ?? "none"}</td>
                      <td className="muted">
                        <code>{why(f.why)}</code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        ) : null,
      )}
    </>
  );
}
