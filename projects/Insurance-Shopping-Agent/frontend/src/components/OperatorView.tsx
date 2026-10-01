import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { OperatorCase } from "../types";

interface Props {
  token: string;
  onError: (e: unknown) => void;
}

export function OperatorView({ token, onError }: Props) {
  const [cases, setCases] = useState<{ id: string; customer_id: string; status: string; version: number; review_reason: string | null }[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<OperatorCase | null>(null);
  const [metrics, setMetrics] = useState<Record<string, unknown> | null>(null);
  const [hours, setHours] = useState(2);

  const refresh = useCallback(async () => {
    try {
      setCases(await api.operatorCases(token));
      setMetrics(await api.operatorMetrics(token));
      if (selected) setDetail(await api.operatorCase(token, selected));
    } catch (e) {
      onError(e);
    }
  }, [token, selected, onError]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const act = async (fn: () => Promise<unknown>) => {
    try {
      await fn();
      await refresh();
    } catch (e) {
      onError(e);
    }
  };

  return (
    <div className="layout" style={{ gridTemplateColumns: "300px 1fr" }}>
      <aside>
        <div className="panel">
          <h2>Controls (mock environment)</h2>
          <div className="row">
            <button onClick={() => act(() => api.workerRunOnce(token))}>Run worker once</button>
          </div>
          <div className="row" style={{ marginTop: 8 }}>
            <input type="number" value={hours} onChange={(e) => setHours(Number(e.target.value))} style={{ width: 80 }} />
            <button onClick={() => act(() => api.advanceClock(token, { hours }))}>Advance clock (h)</button>
          </div>
        </div>
        <div className="panel">
          <h2>Cases</h2>
          <ul className="list">
            {cases.map((c) => (
              <li key={c.id} className={c.id === selected ? "active" : ""} onClick={() => setSelected(c.id)}>
                <span className="badge state">{c.status}</span> <span className="muted">v{c.version}</span>
                <div className="muted">
                  {c.id} · {c.customer_id}
                </div>
                {c.review_reason && <div className="bad">{c.review_reason}</div>}
              </li>
            ))}
          </ul>
        </div>
        {metrics && (
          <div className="panel">
            <h2>Metrics</h2>
            <pre>{JSON.stringify({ ...metrics, capability_matrix: undefined }, null, 1)}</pre>
            <h3>Capability matrix</h3>
            <pre>{JSON.stringify(metrics["capability_matrix"], null, 1)}</pre>
          </div>
        )}
      </aside>
      <main>
        {!detail && <div className="panel muted">Select a case to inspect adapter requests, transitions, pending actions and redacted evidence.</div>}
        {detail && (
          <>
            <div className="panel">
              <h2>
                {detail.id} <span className="badge state">{detail.status}</span> v{detail.version}
              </h2>
              {detail.review_reason && <div className="bad">{detail.review_reason}</div>}
              {detail.status === "manual_review" && (
                <div className="row" style={{ marginTop: 8 }}>
                  {["resume_underwriting", "back_to_selection", "decline", "back_to_collecting"].map((r) => (
                    <button key={r} onClick={() => act(() => api.resolveReview(token, detail.id, r))}>
                      {r.replace(/_/g, " ")}
                    </button>
                  ))}
                  <span className="muted">Operators cannot approve actions or mark a case completed.</span>
                </div>
              )}
              {detail.pending_action && (
                <p>
                  Pending action <strong>{detail.pending_action.type}</strong> ({detail.pending_action.status}) hash <span className="hash">{detail.pending_action.payload_hash}</span>
                </p>
              )}
            </div>
            <div className="panel">
              <h2>State transitions</h2>
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Event</th>
                    <th>Actor</th>
                    <th>From → To</th>
                    <th>At</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.events.map((e) => (
                    <tr key={e.sequence}>
                      <td>{e.sequence}</td>
                      <td>{e.type}</td>
                      <td className="muted">{e.actor}</td>
                      <td>
                        {e.from} → {e.to}
                      </td>
                      <td className="muted">{e.at.slice(0, 19)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="panel">
              <h2>Adapter requests</h2>
              <table>
                <thead>
                  <tr>
                    <th>Insurer</th>
                    <th>Operation</th>
                    <th>Request ref</th>
                    <th>Env</th>
                    <th>Latency</th>
                    <th>Outcome</th>
                  </tr>
                </thead>
                <tbody>
                  {detail.provider_requests.map((p) => (
                    <tr key={p.id}>
                      <td>{p.insurer_id}</td>
                      <td>{p.operation}</td>
                      <td className="muted">{p.request_ref ?? "—"}</td>
                      <td>{p.environment}</td>
                      <td>{p.latency_ms} ms</td>
                      <td className={p.outcome === "ok" ? "ok" : "bad"}>{p.outcome}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="grid2">
              <div className="panel">
                <h2>Tool runs</h2>
                <table>
                  <tbody>
                    {detail.tool_runs.map((t) => (
                      <tr key={t.id}>
                        <td>{t.tool}</td>
                        <td className="muted">{t.model_version}</td>
                        <td>{t.latency_ms} ms</td>
                        <td className={t.outcome === "ok" ? "ok" : "bad"}>{t.outcome}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="panel">
                <h2>Jobs</h2>
                <table>
                  <tbody>
                    {detail.jobs.map((j) => (
                      <tr key={j.id}>
                        <td>{j.type}</td>
                        <td>
                          <span className="badge">{j.status}</span>
                        </td>
                        <td className="muted">attempts {j.attempts}</td>
                        <td className="bad">{j.last_error ?? ""}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
            <div className="panel">
              <h2>Redacted evidence</h2>
              <pre>{JSON.stringify({ needs: detail.needs, application: detail.application, policy: detail.policy }, null, 2)}</pre>
            </div>
          </>
        )}
      </main>
    </div>
  );
}
