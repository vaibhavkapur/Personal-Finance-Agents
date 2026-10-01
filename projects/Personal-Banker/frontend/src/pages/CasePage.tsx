import { useCallback, useEffect, useState } from "react";
import { api, ApiError, AgentTurn, CaseDetail, Option, Timeline, money, pct } from "../api";
import { ProjectionChart } from "../components/ProjectionChart";
import { StatePill, Stepper } from "../components/StatePill";
import { nextDecision } from "./InboxPage";

export function CasePage({ id }: { id: string }) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    try {
      const [d, t] = await Promise.all([api.getCase(id), api.timeline(id)]);
      setDetail(d);
      setTimeline(t);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [id]);

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 4000);
    return () => clearInterval(t);
  }, [refresh]);

  async function act<T>(fn: () => Promise<T>): Promise<T | undefined> {
    setBusy(true);
    setError(null);
    try {
      const r = await fn();
      await refresh();
      return r;
    } catch (e) {
      const err = e as ApiError;
      setError(`${err.code ? err.code + ": " : ""}${err.message}${err.details && Object.keys(err.details).length ? " " + JSON.stringify(err.details) : ""}`);
      await refresh();
      return undefined;
    } finally {
      setBusy(false);
    }
  }

  if (!detail) return <p className="muted">{error || "Loading…"}</p>;
  const canEvaluate = ["collecting", "evaluating", "needs_information", "needs_requote", "awaiting_approval"].includes(detail.state);
  const canCancel = ["collecting", "evaluating", "needs_information", "awaiting_approval", "needs_requote", "manual_review"].includes(detail.state);

  return (
    <div className="grid">
      <section className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div className="row">
            <h2 style={{ margin: 0 }}>Case <span className="mono">{detail.id}</span></h2>
            <StatePill state={detail.state} />
            <span className="muted small">version {detail.version} · simulated now {detail.now}</span>
          </div>
          <div className="row">
            {canEvaluate && <button disabled={busy} onClick={() => act(() => api.evaluate(id))}>{detail.plan ? "Re-evaluate" : "Evaluate options"}</button>}
            {canCancel && <button className="danger" disabled={busy} onClick={() => act(() => api.cancel(id))}>Cancel case</button>}
            <a href="#/inbox" className="small">Back to inbox</a>
          </div>
        </div>
        <div style={{ marginTop: 10 }}><Stepper state={detail.state} /></div>
        <p className="small" style={{ marginTop: 10 }}><strong>Next decision:</strong> {nextDecision(detail)}</p>
        {detail.review_reason && ["manual_review", "rejected", "needs_requote", "outcome_unknown"].includes(detail.state) && <div className="warning">{detail.review_reason}</div>}
        {detail.warnings.map((w, i) => <div key={i} className="warning">{w}</div>)}
        {error && <div className="error">{error}</div>}
      </section>

      <div className="grid two">
        <Snapshot detail={detail} />
        <Questions detail={detail} busy={busy} onAnswer={(body) => act(() => api.answer(id, body))} />
      </div>

      {detail.plan && detail.state !== "completed" && (
        <Comparison detail={detail} busy={busy} onPrepare={(optionId, amount) => act(() => api.prepare(id, optionId, amount))} />
      )}

      {detail.review && ["awaiting_approval", "approved", "submitted", "outcome_unknown", "verifying", "completed", "manual_review", "rejected"].includes(detail.state) && (
        <ReviewPanel detail={detail} busy={busy} onApprove={async () => {
          await act(async () => {
            const ch = await api.challenge(detail.review!.action_id);
            return api.approve(detail.review!.action_id, {
              expected_case_version: ch.expected_case_version,
              action_payload_hash: ch.action_payload_hash,
              approval_challenge_id: ch.approval_challenge_id,
            });
          });
        }} />
      )}

      <div className="grid two">
        <Tracking detail={detail} timeline={timeline} />
        <AgentChat caseId={id} onChange={refresh} />
      </div>
    </div>
  );
}

function Snapshot({ detail }: { detail: CaseDetail }) {
  return (
    <section className="panel">
      <h2>Verified account snapshot</h2>
      <table>
        <thead><tr><th>Account</th><th className="num">Available</th><th className="num">Current</th><th>Snapshot</th></tr></thead>
        <tbody>
          {detail.accounts.map((a) => (
            <tr key={a.id}>
              <td>
                {a.display_name}<br />
                <span className="small muted">{a.provider_id} · {a.account_kind}</span>{" "}
                {a.ownership_verified ? <span className="pill ok">owner verified</span> : <span className="pill bad">unverified</span>}
                {a.access_revoked && <span className="pill bad">access revoked</span>}
                {a.pending.length > 0 && <div className="small muted">pending: {a.pending.map((p) => `${p.description} ${money(p.amount_minor)} (${p.expected_on})`).join("; ")} — not spendable</div>}
              </td>
              <td className="num">{money(a.available_minor, a.currency)}</td>
              <td className="num muted">{money(a.current_minor, a.currency)}</td>
              <td className="small muted">{a.snapshot_at}<br />{a.snapshot_source}{a.evidence_id ? ` · ${a.evidence_id}` : ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <h3>Maturing deposit</h3>
      <dl className="kv">
        <dt>Contract</dt><dd className="mono">{detail.deposit.id} · {detail.deposit.contract_version} · {detail.deposit.evidence_id}</dd>
        <dt>Principal</dt><dd>{money(detail.deposit.principal_minor)} at {pct(detail.deposit.apy_decimal)}</dd>
        <dt>Matures</dt><dd>{detail.deposit.maturity_date} (instruction deadline {detail.deposit.renewal_instruction_deadline})</dd>
        <dt>Bank default</dt><dd>{detail.deposit.default_maturity_behavior.replaceAll("_", " ")}</dd>
      </dl>
      <h3>Obligations projected</h3>
      <table>
        <tbody>
          {detail.obligations.map((o) => (
            <tr key={o.id}>
              <td>{o.description}</td>
              <td className="num">{money(o.amount_minor)}</td>
              <td>{o.due_date}</td>
              <td><span className={`pill ${o.certainty === "confirmed" ? "ok" : "warn"}`}>{o.certainty}</span></td>
              <td className="mono muted small">{o.evidence_id || "user entered"}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function Questions({ detail, busy, onAnswer }: { detail: CaseDetail; busy: boolean; onAnswer: (body: Record<string, unknown>) => Promise<unknown> }) {
  const [lockup, setLockup] = useState("");
  const [includes, setIncludes] = useState("");
  const [buffer, setBuffer] = useState("");
  const [desc, setDesc] = useState("");
  const [amount, setAmount] = useState("");
  const [due, setDue] = useState("");
  const editable = ["collecting", "needs_information", "evaluating", "needs_requote"].includes(detail.state);
  return (
    <section className="panel">
      <h2>Your inputs {detail.outstanding_questions.length > 0 && <span className="pill warn">{detail.outstanding_questions.length} outstanding</span>}</h2>
      <dl className="kv">
        <dt>Minimum buffer</dt><dd>{money(detail.minimum_buffer_minor)}</dd>
        <dt>Reserve includes bills</dt><dd>{detail.buffer_includes_obligations === null ? <span className="muted">not answered</span> : detail.buffer_includes_obligations ? "yes" : "no"}</dd>
        <dt>Preferred lock-up</dt><dd>{detail.preferred_lockup_days === null ? <span className="muted">not answered</span> : `${detail.preferred_lockup_days} days`}</dd>
      </dl>
      {detail.outstanding_questions.map((q) => (
        <div key={q.field} className="notice"><strong>{q.question}</strong><br /><span className="small">{q.why}</span></div>
      ))}
      {editable && (
        <div>
          <div className="grid two">
            <div>
              <label>Preferred lock-up (days)</label>
              <input value={lockup} onChange={(e) => setLockup(e.target.value)} inputMode="numeric" placeholder={detail.preferred_lockup_days?.toString() ?? "e.g. 365"} />
            </div>
            <div>
              <label>Reserve already includes dated bills?</label>
              <select value={includes} onChange={(e) => setIncludes(e.target.value)}>
                <option value="">— unchanged —</option>
                <option value="yes">Yes</option>
                <option value="no">No</option>
              </select>
            </div>
          </div>
          <label>Change minimum buffer (USD)</label>
          <input value={buffer} onChange={(e) => setBuffer(e.target.value)} inputMode="decimal" placeholder={(detail.minimum_buffer_minor / 100).toFixed(2)} />
          <details style={{ marginTop: 8 }}>
            <summary>Add an obligation not in the records</summary>
            <div className="grid two">
              <div><label>Description</label><input value={desc} onChange={(e) => setDesc(e.target.value)} /></div>
              <div><label>Amount (USD)</label><input value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" /></div>
            </div>
            <label>Due date</label>
            <input type="date" value={due} onChange={(e) => setDue(e.target.value)} />
          </details>
          <div style={{ marginTop: 10 }}>
            <button
              className="primary"
              disabled={busy}
              onClick={async () => {
                const body: Record<string, unknown> = {};
                if (lockup !== "") body.preferred_lockup_days = parseInt(lockup, 10);
                if (includes !== "") body.buffer_includes_obligations = includes === "yes";
                if (buffer !== "") body.minimum_buffer_minor = Math.round(parseFloat(buffer) * 100);
                if (desc && amount && due) body.obligations = [{ description: desc, amount_minor: Math.round(parseFloat(amount) * 100), due_date: due, certainty: "confirmed" }];
                await onAnswer(body);
                setLockup(""); setIncludes(""); setBuffer(""); setDesc(""); setAmount(""); setDue("");
              }}
            >
              Save answers
            </button>
            <span className="muted small" style={{ marginLeft: 8 }}>Then evaluate (or re-evaluate) to refresh the comparison.</span>
          </div>
        </div>
      )}
    </section>
  );
}

function Comparison({ detail, busy, onPrepare }: { detail: CaseDetail; busy: boolean; onPrepare: (optionId: string, amount?: number) => Promise<unknown> }) {
  const plan = detail.plan!;
  const [amount, setAmount] = useState("");
  const canPrepare = detail.state === "evaluating" && detail.missing_fields.length === 0;
  const reserved = detail.deposit.principal_minor - plan.max_lockable_minor;
  return (
    <section className="panel">
      <h2>Allocation comparison <span className="muted small">plan {plan.id} v{plan.version} · inputs {plan.inputs_hash.slice(0, 20)}…</span></h2>
      <div className="grid two">
        <div>
          <dl className="kv">
            <dt>Principal at maturity</dt><dd>{money(detail.deposit.principal_minor)} on {detail.deposit.maturity_date}</dd>
            <dt>Stays available</dt><dd>{money(reserved)} (buffer {money(plan.projection.buffer_minor)} plus dated bills)</dd>
            <dt>Can be placed</dt><dd><strong>{money(plan.max_lockable_minor)}</strong></dd>
            <dt>Lowest projected cash</dt><dd>{money(plan.projection.lowest_from_effective_minor)} on {plan.projection.lowest_from_effective_date}</dd>
          </dl>
          {plan.projection.pre_effective_breaches.length > 0 && (
            <div className="warning">Before maturity the projection dips to {money(plan.projection.pre_effective_breaches[0].balance_minor)} on {plan.projection.pre_effective_breaches[0].date}; the locked CD cannot fund that.</div>
          )}
        </div>
        <ProjectionChart projection={plan.projection} />
      </div>
      <h3>Options over a common 365-day horizon on {money(plan.max_lockable_minor)}</h3>
      {canPrepare && (
        <div className="row small" style={{ marginBottom: 8 }}>
          <span className="muted">Amount to place (blank = maximum {money(plan.max_lockable_minor)}):</span>
          <input style={{ width: 140 }} value={amount} onChange={(e) => setAmount(e.target.value)} inputMode="decimal" placeholder={(plan.max_lockable_minor / 100).toFixed(2)} />
        </div>
      )}
      {plan.options.map((o) => (
        <OptionCard key={o.offer_id} o={o} canPrepare={canPrepare} busy={busy} onPrepare={() => onPrepare(o.offer_id, amount ? Math.round(parseFloat(amount) * 100) : undefined)} />
      ))}
      <h3>Assumptions and sources</h3>
      <ul className="small">
        {plan.assumptions.map((a, i) => (
          <li key={i}>{a.text} <span className="mono muted">{JSON.stringify(a.source)}</span></li>
        ))}
      </ul>
    </section>
  );
}

function OptionCard({ o, canPrepare, busy, onPrepare }: { o: Option; canPrepare: boolean; busy: boolean; onPrepare: () => void }) {
  const offer = o.offer;
  return (
    <div className={`option ${o.comparable ? "" : "excluded"}`}>
      <div className="head">
        <div>
          <strong>{offer.product_name}</strong> <span className="muted small">{offer.provider_id} · {offer.offer_kind.replaceAll("_", " ")}</span>
        </div>
        <div className="row">
          <span className="pill">{pct(offer.apy_decimal)} APY {offer.rate_type}</span>
          {o.comparable ? <span className="pill ok">comparable</span> : <span className="pill bad">{o.exclusion_reasons.join(", ")}</span>}
        </div>
      </div>
      <dl className="kv" style={{ marginTop: 8 }}>
        <dt>Locked funds</dt><dd>{o.liquid ? "None — liquid" : `until ${o.locked_until} (${offer.term_days} days)`}</dd>
        <dt>Fees</dt><dd>{money(o.fees_minor)}</dd>
        <dt>Earnings at horizon</dt><dd>{o.comparable ? <>{money(o.earnings_at_horizon_minor)} → <strong>{money(o.net_at_horizon_minor)}</strong> net</> : <span className="muted">not computed: terms not comparable</span>}</dd>
        <dt>Eligibility</dt><dd>{offer.eligibility_status}{offer.eligibility_notes ? ` — ${offer.eligibility_notes}` : ""}</dd>
        <dt>Restrictions</dt><dd className="small">{Object.entries(offer.restrictions).map(([k, v]) => `${k}: ${String(v)}`).join("; ") || "none recorded"}</dd>
        <dt>Source</dt><dd className="mono small">{o.source.provider_id} · v{o.source.product_version} · retrieved {o.source.retrieved_at} · {o.source.environment}{o.source.authoritative ? " · authoritative" : " · estimated"} · valid until {offer.valid_until}</dd>
      </dl>
      {o.early_withdrawal_note && <p className="small muted">{o.early_withdrawal_note}</p>}
      {o.warnings.map((w, i) => <div key={i} className="warning">{w}</div>)}
      {o.assumptions.length > 0 && <p className="small muted">Assumes: {o.assumptions.join("; ")}</p>}
      {canPrepare && o.comparable && (
        <button className="primary" disabled={busy} onClick={onPrepare}>Prepare for approval</button>
      )}
    </div>
  );
}

function ReviewPanel({ detail, busy, onApprove }: { detail: CaseDetail; busy: boolean; onApprove: () => Promise<void> }) {
  const r = detail.review!;
  const approvable = detail.state === "awaiting_approval" && r.action_status === "proposed";
  return (
    <section className="panel" style={{ borderColor: approvable ? "var(--accent)" : undefined }}>
      <h2>{approvable ? "Review and approve the exact instruction" : "Approved instruction"} <span className="muted small">action {r.action_id} · {r.action_status}</span></h2>
      <div className="grid two">
        <dl className="kv">
          <dt>Instruction</dt><dd><strong>{r.instruction.instruction_type.replaceAll("_", " ")}</strong> of <strong>{r.amount_display}</strong> effective {r.instruction.effective_at}</dd>
          <dt>From</dt><dd>{r.source_account.display_name} <span className="mono muted">{r.source_account.id}</span></dd>
          <dt>To</dt><dd>{r.destination_account ? <>{r.destination_account.display_name} <span className="mono muted">{r.destination_account.id}</span> {r.destination_account.ownership_verified && <span className="pill ok">same owner, verified</span>}</> : "—"}</dd>
          {r.offer && <>
            <dt>Terms</dt><dd>{r.offer.product_name}, {pct(r.offer.apy_decimal)} APY, {r.offer.term_days ? `${r.offer.term_days} days` : "no lock-up"}, fees {money(r.offer.fees_minor)}<br /><span className="mono small muted">version {r.offer.product_version} · {r.offer.source.provider_id} · retrieved {r.offer.source.retrieved_at} · {r.offer.source.environment} · valid until {r.offer.valid_until}</span></dd>
          </>}
          <dt>Irreversible effect</dt><dd className="warning" style={{ margin: 0 }}>{r.irreversible_effect}</dd>
          {r.liquidity && <><dt>Liquidity after</dt><dd>lowest projected cash {money(r.liquidity.lowest_balance_after_allocation_minor)} vs buffer {money(r.liquidity.buffer_minor)}; {money(r.liquidity.reserved_minor)} stays available</dd></>}
          <dt>Payload hash</dt><dd className="mono small">{r.action_payload_hash}</dd>
          <dt>Idempotency</dt><dd className="mono small">{r.idempotency_key} · {r.request_ref}</dd>
        </dl>
        <div>
          <h3>Documents</h3>
          <ul className="small">
            {r.documents.map((d) => <li key={d.id}>{d.summary || d.id} <span className="mono muted">{d.id} · {d.content_hash}</span></li>)}
          </ul>
          <h3>Assumptions you are confirming</h3>
          <ul className="small">{r.assumptions_requiring_confirmation.map((a, i) => <li key={i}>{a.text}</li>)}</ul>
          {r.approvals.length > 0 && (
            <>
              <h3>Approvals</h3>
              <ul className="small">{r.approvals.map((a) => <li key={a.id} className="mono">{a.id} by {a.approver_id} · expires {a.expires_at}{a.consumed_at ? ` · consumed ${a.consumed_at}` : ""}{a.revoked_at ? ` · revoked ${a.revoked_at}` : ""}</li>)}</ul>
            </>
          )}
        </div>
      </div>
      {approvable && (
        <div style={{ marginTop: 12 }} className="row">
          <button className="primary" disabled={busy} onClick={onApprove}>Approve exactly this instruction</button>
          <span className="muted small">Approval binds to the payload hash and case version {r.case_version}. It is invalidated if the amount, destination, term, fee or offer version changes before execution.</span>
        </div>
      )}
    </section>
  );
}

function Tracking({ detail, timeline }: { detail: CaseDetail; timeline: Timeline | null }) {
  const instr = detail.instruction;
  return (
    <section className="panel">
      <h2>Effective-date tracking and verification</h2>
      {instr ? (
        <dl className="kv">
          <dt>Instruction</dt><dd>{instr.instruction_type.replaceAll("_", " ")} · <StatePill state={instr.status} /></dd>
          <dt>Amount</dt><dd>{money(instr.amount_minor, instr.currency)} effective {instr.effective_at}</dd>
          <dt>Bank reference</dt><dd className="mono">{instr.external_ref || <span className="muted">none yet</span>}</dd>
          <dt>Request reference</dt><dd className="mono">{instr.request_ref}</dd>
          {detail.completion_evidence_ref && <><dt>Completion evidence</dt><dd className="mono">{detail.completion_evidence_ref}</dd></>}
        </dl>
      ) : (
        <p className="muted small">No instruction prepared yet.</p>
      )}
      {instr?.reconciliation && (
        <>
          <h3>Reconciliation {instr.reconciliation.matched ? <span className="pill ok">matched</span> : <span className="pill bad">exception</span>}</h3>
          <table>
            <thead><tr><th>Check</th><th>Expected</th><th>Actual</th><th></th></tr></thead>
            <tbody>
              {instr.reconciliation.checks.map((c) => (
                <tr key={c.check}>
                  <td>{c.check}</td>
                  <td className="mono small">{String(c.expected)}</td>
                  <td className="mono small">{String(c.actual)}</td>
                  <td>{c.ok ? <span className="pill ok">ok</span> : <span className="pill bad">mismatch</span>}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <h3>Verified balances (from latest bank snapshot)</h3>
      <table>
        <tbody>
          {detail.accounts.map((a) => (
            <tr key={a.id}><td>{a.display_name}</td><td className="num">{money(a.available_minor)}</td><td className="small muted">{a.snapshot_at} · {a.snapshot_source}</td></tr>
          ))}
        </tbody>
      </table>
      <h3>Timeline</h3>
      <ul className="timeline" style={{ listStyle: "none", padding: 0, margin: 0, maxHeight: 320, overflow: "auto" }}>
        {timeline?.events.slice().reverse().map((e) => (
          <li key={e.id}>
            <span className="when">{e.occurred_at}</span>
            <strong>{e.type}</strong>
            {e.previous_state && <span className="muted"> {e.previous_state} → {e.next_state}</span>}
            <span className="muted"> · {e.actor}</span>
            {e.data && Object.keys(e.data).length > 0 && <div className="mono small muted">{JSON.stringify(e.data).slice(0, 220)}</div>}
          </li>
        ))}
      </ul>
      {timeline && timeline.provider_requests.length > 0 && (
        <details>
          <summary>Provider requests ({timeline.provider_requests.length})</summary>
          <table>
            <tbody>
              {timeline.provider_requests.map((r) => (
                <tr key={r.id}><td className="small">{r.created_at}</td><td>{r.provider_id}</td><td>{r.operation}</td><td><span className={`pill ${r.outcome === "ok" ? "ok" : "warn"}`}>{r.outcome}</span></td><td className="mono small muted">{r.request_ref}</td></tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </section>
  );
}

function AgentChat({ caseId, onChange }: { caseId: string; onChange: () => Promise<void> }) {
  const [messages, setMessages] = useState<{ role: "user" | "agent"; text: string; turn?: AgentTurn }[]>([]);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    if (!input.trim()) return;
    const text = input;
    setInput("");
    setMessages((m) => [...m, { role: "user", text }]);
    setBusy(true);
    try {
      const turn = await api.agentTurn(text, caseId);
      setMessages((m) => [...m, { role: "agent", text: turn.reply, turn }]);
      await onChange();
    } catch (e) {
      setMessages((m) => [...m, { role: "agent", text: `Error: ${(e as Error).message}` }]);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="panel">
      <h2>Assistant <span className="muted small">reads the case record each turn; cannot approve or execute</span></h2>
      <div className="chat">
        {messages.length === 0 && <p className="muted small">Try: "Yes, the $3,000 includes the rent. I can lock money up for a year." or "What's the status?"</p>}
        {messages.map((m, i) => (
          <div key={i} className={`msg ${m.role}`}>
            {m.text}
            {m.turn && (
              <div className="meta">
                tools: {m.turn.tool_calls.map((c) => `${c.tool}${c.outcome === "error" ? "!" : ""}`).join(", ") || "none"} · state {m.turn.state ?? "—"} · {m.turn.model_version}
                {m.turn.refused && " · refused"}{m.turn.escalated && " · escalated"}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="row" style={{ marginTop: 8 }}>
        <input value={input} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => e.key === "Enter" && send()} placeholder="Message the assistant" style={{ flex: 1 }} />
        <button className="primary" disabled={busy} onClick={send}>Send</button>
      </div>
    </section>
  );
}
