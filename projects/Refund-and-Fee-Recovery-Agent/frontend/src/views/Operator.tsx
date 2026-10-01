import { Fragment, useCallback, useEffect, useState } from "react";
import { ApiError, api } from "../api";

export function Operator() {
  const [ov, setOv] = useState<any | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [days, setDays] = useState(1);

  const refresh = useCallback(() => api.ops.overview().then(setOv).catch((e: ApiError) => setError(e.message)), []);
  useEffect(() => {
    refresh();
  }, [refresh]);

  const act = async (fn: () => Promise<unknown>) => {
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as ApiError).message);
    }
  };

  if (!ov) return <p>{error ?? "Loading…"}</p>;

  return (
    <section>
      <h2>Operator view <span className="muted">{ov.environment} · {ov.now}</span></h2>
      {error && <div className="error">{error}</div>}
      <div className="actions">
        <button onClick={() => act(api.ops.runWorker)}>Run worker + deliver simulator callbacks</button>
        <label>Advance fixture clock <input type="number" min={0} step={0.5} value={days} onChange={(e) => setDays(Number(e.target.value))} /> days</label>
        <button onClick={() => act(() => api.ops.advance(days))}>Advance</button>
      </div>

      <div className="grid">
        <div className="card">
          <h3>Metrics</h3>
          <dl>
            {Object.entries(ov.metrics).map(([k, v]) => (
              <Fragment key={k}>
                <dt>{k}</dt>
                <dd>{String(v)}</dd>
              </Fragment>
            ))}
          </dl>
        </div>
        <div className="card">
          <h3>Adapter capabilities</h3>
          <table>
            <thead><tr><th>Provider</th><th>Env</th><th>open</th><th>followup</th><th>get</th><th>find_action</th></tr></thead>
            <tbody>
              {Object.values<any>(ov.capabilities).map((c) => (
                <tr key={c.provider}><td>{c.provider}</td><td>{c.environment}</td><td>{String(c.open_case)}</td><td>{String(c.send_followup)}</td><td>{String(c.get_case)}</td><td>{String(c.find_action)}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>Cases</h3>
        <table>
          <thead><tr><th>Case</th><th>Status</th><th>v</th><th>Target</th><th>Final</th><th>Provisional</th><th>Store</th><th></th></tr></thead>
          <tbody>
            {ov.cases.map((c: any) => (
              <tr key={c.id}>
                <td><code>{c.id}</code></td><td><span className={`state ${c.status}`}>{c.status}</span></td><td>{c.version}</td>
                <td>{c.target_minor}</td><td>{c.final_recovered_minor}</td><td>{c.provisional_minor}</td><td>{c.store_credit_minor}</td>
                <td>
                  {c.status === "manual_review" && (
                    <button onClick={() => act(() => api.ops.release(c.id, "investigating", "operator reviewed evidence; resume investigation"))}>Release to investigating</button>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid">
        <div className="card">
          <h3>Pending actions</h3>
          <table>
            <thead><tr><th>Action</th><th>Type</th><th>Status</th><th>Request ref</th></tr></thead>
            <tbody>{ov.pending_actions.map((a: any) => <tr key={a.id}><td><code>{a.id}</code></td><td>{a.type}</td><td>{a.status}</td><td><code>{a.request_ref}</code></td></tr>)}</tbody>
          </table>
        </div>
        <div className="card">
          <h3>Jobs (leases)</h3>
          <table>
            <thead><tr><th>Type</th><th>Status</th><th>Run at</th><th>Attempts</th><th>Lease</th></tr></thead>
            <tbody>{ov.jobs.map((j: any) => <tr key={j.id}><td>{j.type}</td><td>{j.status}</td><td>{j.run_at}</td><td>{j.attempts}</td><td>{j.lease_owner ?? ""}</td></tr>)}</tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>Adapter requests</h3>
        <table>
          <thead><tr><th>Provider</th><th>Env</th><th>Operation</th><th>Request ref</th><th>Status</th><th>Started</th><th>Summary</th></tr></thead>
          <tbody>
            {ov.provider_requests.map((r: any) => (
              <tr key={r.id}><td>{r.provider}</td><td>{r.environment}</td><td>{r.operation}</td><td><code>{r.request_ref}</code></td><td>{r.status}</td><td>{r.started_at}</td><td className="small">{JSON.stringify(r.response_summary)}</td></tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid">
        <div className="card">
          <h3>Recent transitions</h3>
          <table>
            <tbody>{ov.recent_transitions.map((e: any) => <tr key={e.id}><td className="muted">{e.occurred_at}</td><td><code>{e.case_id}</code></td><td>{e.previous_state} → <strong>{e.next_state}</strong></td><td className="muted">{e.actor}</td></tr>)}</tbody>
          </table>
        </div>
        <div className="card">
          <h3>Provider event inbox</h3>
          <table>
            <thead><tr><th>Provider</th><th>Event</th><th>Type</th><th>Outcome</th><th></th></tr></thead>
            <tbody>
              {ov.inbox.map((e: any) => (
                <tr key={e.provider + e.event_id}><td>{e.provider}</td><td><code>{e.event_id}</code></td><td>{e.event_type}</td><td className="small">{e.outcome}</td>
                  <td><button className="secondary" onClick={() => act(() => api.ops.replay(e.provider, e.event_id))}>Replay</button></td></tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="card">
        <h3>Simulator pending callbacks</h3>
        <pre className="small">{JSON.stringify(ov.simulator_pending, null, 1)}</pre>
      </div>
    </section>
  );
}
