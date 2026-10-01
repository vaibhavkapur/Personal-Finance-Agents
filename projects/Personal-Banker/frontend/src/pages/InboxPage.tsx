import { useEffect, useState } from "react";
import { api, ApiError, CUSTOMER_ID, Inbox, CaseSummary, money, pct } from "../api";
import { navigate } from "../App";
import { StatePill } from "../components/StatePill";

export function InboxPage() {
  const [inbox, setInbox] = useState<Inbox | null>(null);
  const [cases, setCases] = useState<CaseSummary[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [buffer, setBuffer] = useState("1000");
  const [lockup, setLockup] = useState<string>("");
  const [includes, setIncludes] = useState<string>("unknown");
  const [selected, setSelected] = useState<Record<string, boolean>>({});
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const [i, c] = await Promise.all([api.inbox(), api.cases()]);
      setInbox(i);
      setCases(c);
      setSelected((prev) => {
        if (Object.keys(prev).length) return prev;
        const next: Record<string, boolean> = {};
        for (const o of i.obligations) next[o.id] = o.certainty === "confirmed";
        return next;
      });
    } catch (e) {
      setError(String((e as Error).message));
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function create(depositId: string) {
    setBusy(true);
    setError(null);
    try {
      const body = {
        customer_id: CUSTOMER_ID,
        deposit_id: depositId,
        currency: "USD",
        minimum_buffer_minor: Math.round(parseFloat(buffer || "0") * 100),
        obligation_ids: Object.entries(selected).filter(([, v]) => v).map(([k]) => k),
        preferred_lockup_days: lockup === "" ? null : parseInt(lockup, 10),
        buffer_includes_obligations: includes === "unknown" ? null : includes === "yes",
      };
      const res = await api.createCase(body);
      navigate(`/case/${res.id}`);
    } catch (e) {
      const err = e as ApiError;
      setError(err.code === "case_already_open" ? `A case is already open for this deposit (${String(err.details.case_id)}).` : err.message);
    } finally {
      setBusy(false);
    }
  }

  if (!inbox) return <p className="muted">{error || "Loading…"}</p>;

  return (
    <div className="grid two">
      <section className="panel">
        <h2>Maturity inbox <span className="muted small">as of {inbox.today}</span></h2>
        {inbox.deposits.map((d) => (
          <div key={d.deposit_id} className="option">
            <div className="head">
              <strong>{d.account.display_name}</strong>
              <span className="pill info">{d.days_to_maturity} days to maturity</span>
            </div>
            <dl className="kv">
              <dt>Principal</dt><dd>{money(d.principal_minor, d.currency)} at {pct(d.apy_decimal)} APY</dd>
              <dt>Matures</dt><dd>{d.maturity_date}</dd>
              <dt>Instruction deadline</dt><dd>{d.renewal_instruction_deadline}</dd>
              <dt>Bank default</dt><dd>{d.default_maturity_behavior.replaceAll("_", " ")}</dd>
              <dt>Contract</dt><dd className="mono">{d.deposit_id} · {d.contract_version}</dd>
            </dl>
            {d.case ? (
              <div className="row" style={{ marginTop: 10 }}>
                <StatePill state={d.case.state} />
                <a href={`#/case/${d.case.id}`}>Open case {d.case.id}</a>
                {d.case.outstanding_questions.length > 0 && <span className="pill warn">{d.case.outstanding_questions.length} outstanding question(s)</span>}
              </div>
            ) : (
              <div style={{ marginTop: 10 }}>
                <h3>Start a maturity case</h3>
                <label>Minimum cash to keep available after maturity (USD)</label>
                <input value={buffer} onChange={(e) => setBuffer(e.target.value)} inputMode="decimal" />
                <label>Dated obligations to preserve</label>
                {inbox.obligations.map((o) => (
                  <div key={o.id} className="row small">
                    <input type="checkbox" style={{ width: "auto" }} checked={!!selected[o.id]} onChange={(e) => setSelected({ ...selected, [o.id]: e.target.checked })} />
                    <span>{o.description} · {money(o.amount_minor)} due {o.due_date}</span>
                    <span className={`pill ${o.certainty === "confirmed" ? "ok" : "warn"}`}>{o.certainty}</span>
                  </div>
                ))}
                <p className="muted small">Confirmed obligations on record are always projected; estimated ones only when selected.</p>
                <label>Preferred lock-up (days, optional — the agent asks if blank)</label>
                <input value={lockup} onChange={(e) => setLockup(e.target.value)} inputMode="numeric" placeholder="e.g. 365, or 0 for liquid" />
                <label>Does the reserve above already include the dated bills?</label>
                <select value={includes} onChange={(e) => setIncludes(e.target.value)}>
                  <option value="unknown">Not sure — ask me if it matters</option>
                  <option value="yes">Yes, it includes them</option>
                  <option value="no">No, keep them separate</option>
                </select>
                <div style={{ marginTop: 10 }}>
                  <button className="primary" disabled={busy} onClick={() => create(d.deposit_id)}>Create case</button>
                </div>
              </div>
            )}
          </div>
        ))}
        {error && <div className="error">{error}</div>}
      </section>
      <section className="panel">
        <h2>Your cases</h2>
        {cases.length === 0 && <p className="muted">No cases yet.</p>}
        <table>
          <thead><tr><th>Case</th><th>State</th><th>Next decision</th><th>Updated</th></tr></thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.id}>
                <td><a href={`#/case/${c.id}`} className="mono">{c.id}</a></td>
                <td><StatePill state={c.state} /></td>
                <td className="small">{nextDecision(c)}</td>
                <td className="small muted">{c.updated_at}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <h3>Obligations on record</h3>
        <table>
          <thead><tr><th>Bill</th><th className="num">Amount</th><th>Due</th><th>Certainty</th><th>Evidence</th></tr></thead>
          <tbody>
            {inbox.obligations.map((o) => (
              <tr key={o.id}>
                <td>{o.description}</td>
                <td className="num">{money(o.amount_minor)}</td>
                <td>{o.due_date}</td>
                <td><span className={`pill ${o.certainty === "confirmed" ? "ok" : "warn"}`}>{o.certainty}</span></td>
                <td className="mono muted">{o.evidence_id || "user entered"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </div>
  );
}

export function nextDecision(c: CaseSummary): string {
  switch (c.state) {
    case "collecting":
    case "needs_information":
      return c.missing_fields.length ? `Answer: ${c.missing_fields.join(", ")}` : "Evaluate options";
    case "evaluating":
      return c.plan_id ? "Choose an option to prepare" : "Evaluate options";
    case "awaiting_approval":
      return "Review and approve the exact instruction";
    case "approved":
      return "Waiting for the executor";
    case "submitted":
      return "Bank accepted; waiting for effective date";
    case "outcome_unknown":
      return "Provider outcome unknown; lookup pending";
    case "verifying":
      return "Reconciling balances";
    case "needs_requote":
      return "Terms changed; re-evaluate and approve again";
    case "manual_review":
      return `Held for review: ${c.review_reason || ""}`;
    case "completed":
      return "Verified";
    default:
      return c.review_reason || "";
  }
}
