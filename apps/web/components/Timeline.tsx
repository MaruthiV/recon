"use client";

import { useEffect, useRef, useState } from "react";
import { api, type TimelineEvent, when } from "@/lib/api";

export function Timeline({ caseId, onEvent }: { caseId: string; onEvent?: (e: TimelineEvent) => void }) {
  const [events, setEvents] = useState<TimelineEvent[]>([]);
  const [transport, setTransport] = useState<"live" | "polling">("live");
  const [engineering, setEngineering] = useState(false);
  const cursor = useRef(0);
  const notify = useRef(onEvent);
  notify.current = onEvent;

  useEffect(() => {
    const add = (e: TimelineEvent) => {
      // the cursor makes reconnects and the polling fallback idempotent
      if (e.id <= cursor.current) return;
      cursor.current = e.id;
      setEvents((prev) => [...prev, e]);
      notify.current?.(e);
    };
    let poll: ReturnType<typeof setInterval> | null = null;
    let es: EventSource | null = new EventSource(`/api/v1/cases/${caseId}/events/stream`);
    es.addEventListener("audit", (m) => add(JSON.parse((m as MessageEvent).data)));
    es.onerror = () => {
      // some proxies buffer sse, polling with the same cursor shows the same timeline
      es?.close();
      es = null;
      setTransport("polling");
      poll = setInterval(async () => {
        try {
          const r = await api<{ events: TimelineEvent[] }>(`/v1/cases/${caseId}/events?after=${cursor.current}`);
          r.events.forEach(add);
        } catch {
          // keep polling, the api may be restarting
        }
      }, 1500);
    };
    return () => {
      es?.close();
      if (poll) clearInterval(poll);
    };
  }, [caseId]);

  const shown = engineering ? events : events.filter((e) => !e.engineering);
  return (
    <div className="panel">
      <div className="row" style={{ justifyContent: "space-between" }}>
        <h2>Execution timeline</h2>
        <span className="muted">
          {transport === "live" ? "live" : "polling"} ·{" "}
          <label>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              checked={engineering}
              onChange={(e) => setEngineering(e.target.checked)}
            />{" "}
            engineering view
          </label>
        </span>
      </div>
      <ul className="timeline">
        {shown.map((e) => (
          <li key={e.id}>
            <span className="at">{when(e.at)}</span>
            <span className={e.engineering ? "eng" : undefined}>{e.label}</span>
            {engineering && (
              <pre>
                {e.kind} #{e.id}
                {e.command_id ? ` · ${e.command_id}` : ""}
                {"\n"}
                {JSON.stringify(e.data, null, 2)}
              </pre>
            )}
          </li>
        ))}
        {shown.length === 0 && <li className="muted">Nothing has happened yet.</li>}
      </ul>
    </div>
  );
}
