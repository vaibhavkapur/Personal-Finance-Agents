import { useCallback, useEffect, useState } from "react";
import { api, Capability, InboxEvent, Ledger, Overview, money } from "../api";
import { StatePill } from "../components/StatePill";

const SUBMIT_MODES = ["normal", "accepted_not_effective", "insufficient_available", "declined", "accepted_before_timeout", "malformed_response", "delayed_callback", "completed_amount_mismatch"];
const OFFER_MODES = ["normal", "changed_rate", "expired_offer"];

export function OperatorPage() {
  const [ov, setOv] = useState<Overview | null>(null);
  const [ledger, setLedger] = useState<Ledger | null>(null);
  const [inbox, setInbox] = useState<InboxEvent[]>([]);
  const [caps, setCaps] = useState<Capability[]>([]);
  const [log, setLog] = useState<string[]>([]);
  const [days, setDays] = useState("1");
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [o, l, i, c] = await Promise.all([api.overview(), api.ledger(), api.inboxEvents(), api.capabilities()]);
      setOv(o); setLedger(l); setInbox(i); setCaps(c);
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, []);
  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  async function run(label: string, fn: () => Promise<unknown>) {
    try {
      const r = await fn();
      setLog((l) => [`${label}: ${JSON.stringify(r).slice(0, 300)}`, ...l].slice(0, 30));
    } catch (e) {
      setLog((l) => [`${label} failed: ${(e as Error).message}`, ...l]);
    }
    await refresh();
  }

  if (!ov) return <p className="muted">{error || "Loading…"}</p>;

  return (
    <div className="grid">
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>Operations <span className="muted small">simulated now {ov.now} · {ov.environment}</span></h2>
          <div className="row">
            <input style={{ width: 60 }} value={days} onChange={(e) => setDays(e.target.value)} inputMode="numeric" />
            <button onClick={() => run("advance clock", () => api.advance(parseInt(days || "0", 10)))}>Advance clock (days)</button>
            <button className="primary" onClick={() => run("run worker", () => api.runWorker())}>Run worker</button>
            <button className="danger" onClick={() => { if (confirm("Reset the database to the seed fixture?")) run("reset", () => api.reset()); }}>Reset fixture</button>
          </div>
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <Stat label="waiting on customer" value={ov.cases_waiting_on_customer} />
          <Stat label="waiting on provider" value={ov.cases_waiting_on_provider} />
          <Stat label="manual review" value={ov.cases_in_manual_review} tone={ov.cases_in_manual_review ? "bad" : ""} />
          <Stat label="queue pending" value={ov.queue.pending} />
          <Stat label="jobs failed" value={ov.queue.failed} tone={ov.queue.failed ? "bad" : ""} />
          <Stat label="duplicate actions prevented" value={ov.duplicate_actions_prevented} />
          <Stat label="tool errors" value={ov.tool_errors} />
        </div>
        <div className="row" style={{ marginTop: 12 }}>
          <label style={{ margin: 0 }}>Next submit mode</label>
          <select style={{ width: 260 }} value={ov.mock_bank.next_submit_mode} onChange={(e) => run("submit mode", () => api.submitMode(e.target.value))}>
            {SUBMIT_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          <label style={{ margin: 0 }}>Offer mode (Harbor 12-month)</label>
          <select style={{ width: 200 }} value={ov.mock_bank.offer_mode} onChange={(e) => run("offer mode", () => api.offerMode(e.target.value))}>
            {OFFER_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
          {ledger?.accounts.filter((a) => a.kind === "fixed_term_deposit").map((a) => (
            <button key={a.id} onClick={() => run("revoke", () => api.revoke(a.id, !a.access_revoked))}>{a.access_revoked ? "Restore" : "Revoke"} access {a.id}</button>
          ))}
        </div>
        {log.length > 0 && <pre className="mono small muted" style={{ maxHeight: 120, overflow: "auto", background: "#fafbfc", padding: 8 }}>{log.join("\n")}</pre>}
        {error && <div className="error">{error}</div>}
      </section>

      <div className="grid two">
        <section className="panel">
          <h2>Cases</h2>
          <table>
            <thead><tr><th>Case</th><th>State</th><th>v</th><th>Reason</th></tr></thead>
            <tbody>
              {ov.cases.map((c) => (
                <tr key={c.id}><td><a href={`#/case/${c.id}`} className="mono">{c.id}</a></td><td><StatePill state={c.state} /></td><td>{c.version}</td><td className="small">{c.review_reason}</td></tr>
              ))}
            </tbody>
          </table>
          <h3>Pending actions</h3>
          {ov.pending_actions.length === 0 ? <p className="muted small">None.</p> : (
            <table>
              <thead><tr><th>Action</th><th>Type</th><th>Status</th><th>Provider ref</th><th>Error</th></tr></thead>
              <tbody>
                {ov.pending_actions.map((a) => (
                  <tr key={a.id}><td className="mono small">{a.id}<br />{a.payload_hash.slice(0, 22)}…</td><td>{a.type}</td><td><StatePill state={a.status} /></td><td className="mono small">{a.provider_reference || "—"}<br />{a.request_ref}</td><td className="small">{a.last_error}</td></tr>
                ))}
              </tbody>
            </table>
          )}
          <h3>Jobs (persisted, leased)</h3>
          <table>
            <thead><tr><th>Type</th><th>Status</th><th>Run at</th><th>Attempts</th><th>Owner / error</th></tr></thead>
            <tbody>
              {ov.jobs.map((j) => (
                <tr key={j.id}><td>{j.type}</td><td><StatePill state={j.status} /></td><td className="small">{j.run_at}</td><td>{j.attempts}</td><td className="small muted">{j.lease_owner}{j.last_error ? ` · ${j.last_error}` : ""}</td></tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="panel">
          <h2>Adapter requests <span className="muted small">redacted</span></h2>
          <table>
            <thead><tr><th>When</th><th>Provider</th><th>Operation</th><th>Outcome</th><th className="num">ms</th></tr></thead>
            <tbody>
              {ov.adapter_requests.map((r) => (
                <tr key={r.id}><td className="small">{r.created_at}</td><td>{r.provider_id}</td><td>{r.operation}<br /><span className="mono small muted">{r.request_ref}</span></td><td><span className={`pill ${r.outcome === "ok" ? "ok" : r.outcome === "declined" ? "bad" : "warn"}`}>{r.outcome}</span></td><td className="num">{r.latency_ms}</td></tr>
              ))}
            </tbody>
          </table>
          <h3>Provider events (inbox)</h3>
          <table>
            <thead><tr><th>Event</th><th>Type</th><th>Signature</th><th>Result</th><th></th></tr></thead>
            <tbody>
              {inbox.map((e) => (
                <tr key={e.id}><td className="mono small">{e.event_id}</td><td>{e.event_type}</td><td>{e.signature_valid ? <span className="pill ok">valid</span> : <span className="pill bad">invalid</span>}</td><td className="small">{e.result}{e.replay_count ? ` (replayed ${e.replay_count})` : ""}</td><td><button onClick={() => run("replay", () => api.replay(e.id))}>Replay</button></td></tr>
              ))}
            </tbody>
          </table>
          <h3>Tool runs</h3>
          <table>
            <thead><tr><th>Tool</th><th>Outcome</th><th className="num">ms</th><th>Model</th></tr></thead>
            <tbody>
              {ov.tool_runs.slice(0, 15).map((t) => (
                <tr key={t.id}><td>{t.tool_name}</td><td><span className={`pill ${t.outcome === "ok" ? "ok" : "bad"}`}>{t.outcome}</span></td><td className="num">{t.latency_ms}</td><td className="small muted">{t.model_version} · {t.prompt_version}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>

      <div className="grid two">
        <section className="panel">
          <h2>Mock bank ledger <span className="muted small">provider side, independent of app snapshots</span></h2>
          <table>
            <thead><tr><th>Account</th><th>Kind</th><th className="num">Available</th><th className="num">Current</th></tr></thead>
            <tbody>
              {ledger?.accounts.map((a) => (
                <tr key={a.id}><td className="mono">{a.id}{a.access_revoked && <span className="pill bad">revoked</span>}</td><td>{a.kind}</td><td className="num">{money(a.available_minor)}</td><td className="num">{money(a.current_minor)}</td></tr>
              ))}
            </tbody>
          </table>
          <h3>Deposits</h3>
          <table>
            <thead><tr><th>Deposit</th><th className="num">Principal</th><th>APY</th><th>Term</th><th>Matures</th><th>Status</th></tr></thead>
            <tbody>
              {ledger?.deposits.map((d) => (
                <tr key={d.id}><td className="mono small">{d.id}<br /><span className="muted">{d.product_version}</span></td><td className="num">{money(d.principal_minor)}</td><td>{d.apy_decimal}</td><td>{d.term_days}d</td><td>{d.maturity_date}</td><td><StatePill state={d.status} /></td></tr>
              ))}
            </tbody>
          </table>
          <h3>Instructions at the bank</h3>
          <table>
            <thead><tr><th>Request ref</th><th>Bank ref</th><th>Status</th><th className="num">Amount</th><th className="num">Credited</th></tr></thead>
            <tbody>
              {ledger?.instructions.map((i) => (
                <tr key={i.request_ref}><td className="mono small">{i.request_ref}</td><td className="mono small">{i.provider_reference}</td><td><StatePill state={i.status} />{i.decline_reason && <span className="small muted"> {i.decline_reason}</span>}</td><td className="num">{money(i.amount_minor)}</td><td className="num">{money(i.credited_amount_minor)}</td></tr>
              ))}
            </tbody>
          </table>
        </section>
        <section className="panel">
          <h2>Provider capability matrix</h2>
          <table>
            <thead><tr><th>Provider</th><th>Env</th><th>Reads</th><th>Offers</th><th>Submit</th><th>Lookup by ref</th><th>Cancel</th><th>Callbacks</th></tr></thead>
            <tbody>
              {caps.map((c) => (
                <tr key={c.provider_id}>
                  <td>{c.provider_id}<br /><span className="small muted">{c.notes}</span></td>
                  <td><span className="pill">{c.environment}</span></td>
                  <td>{yn(c.can_read_snapshots)}</td><td>{yn(c.can_read_offers)}</td><td>{yn(c.can_submit_instructions)}</td><td>{yn(c.can_lookup_by_request_ref)}</td><td>{yn(c.can_cancel_after_acceptance)}</td><td>{yn(c.supports_callbacks)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="small muted">Mock, sandbox and live capabilities are labelled separately. Only the mock provider executes anything; nothing here is a real bank.</p>
        </section>
      </div>
    </div>
  );
}

function Stat({ label, value, tone = "" }: { label: string; value: number; tone?: string }) {
  return <span className={`pill ${tone}`}>{label}: <strong>{value}</strong></span>;
}

function yn(v: boolean) {
  return v ? <span className="pill ok">yes</span> : <span className="pill">no</span>;
}
