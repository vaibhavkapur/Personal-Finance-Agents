import { useCallback, useEffect, useState } from "react";
import { ApiError, api, money } from "../api";
import type { AgentTurn, CaseStatus, Draft, Timeline } from "../types";

export function CaseView({ caseId }: { caseId: string }) {
  const [status, setStatus] = useState<CaseStatus | null>(null);
  const [timeline, setTimeline] = useState<Timeline | null>(null);
  const [turn, setTurn] = useState<AgentTurn | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = useCallback(async () => {
    const [s, t] = await Promise.all([api.getCase(caseId), api.timeline(caseId)]);
    setStatus(s);
    setTimeline(t);
    const pending = t.actions.find((a) => a.status === "awaiting_approval");
    setDraft(pending ? await api.getAction(caseId, pending.id) : null);
  }, [caseId]);

  useEffect(() => {
    refresh().catch((e: ApiError) => setError(e.message));
  }, [refresh]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await refresh();
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  if (!status) return <p>{error ?? "Loading…"}</p>;
  const a = status.amounts;
  const cur = a.currency;
  const merchant = status.channels.find((c) => c.channel_type === "merchant");
  const issuer = status.channels.find((c) => c.channel_type === "issuer");

  return (
    <section>
      <h2>
        {status.order_ref} <span className={`state ${status.status}`}>{status.status}</span> <span className="muted">v{status.version}</span>
      </h2>
      {error && <div className="error">{error}</div>}

      <div className="grid">
        <div className="card">
          <h3>Settlement</h3>
          <dl>
            <dt>Charged</dt><dd>{money(a.requested_minor, cur)}</dd>
            <dt>Promised</dt><dd>{money(a.promised_minor, cur)} {status.promise && <span className="muted">by {status.promise.promised_by} on {status.promise.promised_at.slice(0, 10)}</span>}</dd>
            <dt>Target</dt><dd>{money(a.target_minor, cur)}</dd>
            <dt className="ok">Final credits posted</dt><dd className="ok">{money(a.final_recovered_minor, cur)} <span className="muted">(the only recovered money)</span></dd>
            <dt>Provisional (issuer)</dt><dd>{money(a.provisional_minor, cur)} <span className="muted">shown separately, not recovery</span></dd>
            <dt>Store credit</dt><dd>{money(a.store_credit_minor, cur)} <span className="muted">not a card credit</span></dd>
            <dt>Reversed</dt><dd>{money(a.reversed_minor, cur)}</dd>
            <dt className="warn">Outstanding</dt><dd className="warn">{money(a.outstanding_minor, cur)}</dd>
          </dl>
          {a.overlap_flagged && <div className="error">Possible duplicate recovery: {money(a.overlap_minor, cur)} over target. Held for review.</div>}
          {status.deadline && (
            <p className={status.deadline.alert ? "warn" : "muted"}>
              Dispute deadline (fixture): {status.deadline.deadline_at.slice(0, 10)} {status.deadline.alert && <strong>· {status.deadline.alert}</strong>}
            </p>
          )}
          {status.completion_evidence_ref && <p className="ok">Verified by {status.completion_evidence_ref}</p>}
          {status.outcome_note && <p className="muted">Outcome: {status.outcome_note}</p>}
        </div>

        <div className="card">
          <h3>Next decision</h3>
          <p><strong>{status.next_step}</strong></p>
          {status.pending_question && (
            <div className="question">
              <p>{status.pending_question.prompt}</p>
              {status.pending_question.kind === "ambiguous_credit_match" && status.pending_question.options.map((o, i) => (
                <button key={i} disabled={busy} onClick={() => run(() => api.answer(caseId, { transaction_id: o.transaction_id ?? null }))}>
                  {o.transaction_id ? `${money(o.amount_minor, cur)} on ${String(o.posted_at).slice(0, 10)}` : "None of these"}
                </button>
              ))}
              {status.pending_question.kind === "store_credit_preference" && (
                <>
                  <button disabled={busy} onClick={() => run(() => api.answer(caseId, { accept_store_credit: true }))}>Accept store credit and close</button>
                  <button disabled={busy} onClick={() => run(() => api.answer(caseId, { accept_store_credit: false }))}>Keep pursuing card refund</button>
                </>
              )}
              {status.pending_question.kind === "missing_promise_evidence" && <PromiseForm caseId={caseId} max={a.requested_minor} onDone={refresh} />}
            </div>
          )}
          <div className="actions">
            <button disabled={busy} onClick={() => run(async () => setTurn(await api.agentTurn(caseId)))}>Ask the agent</button>
            <button disabled={busy} onClick={() => run(() => api.reconcile(caseId))}>Re-check account credits</button>
            {status.next_step === "draft_merchant_message" && <button disabled={busy} onClick={() => run(() => api.draftMessage(caseId))}>Draft merchant message</button>}
            {status.next_step === "consider_issuer_dispute" && <button disabled={busy} onClick={() => run(() => api.draftDispute(caseId))}>Draft issuer dispute</button>}
            {!["recovered", "unresolved", "already_refunded", "not_supported"].includes(status.status) && (
              <button disabled={busy} className="secondary" onClick={() => run(() => api.closeUnresolved(caseId, "customer chose to stop pursuing"))}>Stop pursuing (record unresolved)</button>
            )}
          </div>
          {turn && (
            <div className="agent">
              <p className="muted">tools: {turn.tool_calls.map((t) => t.name).join(" → ")}{turn.guardrail.blocked && " · guardrail blocked an unsupported claim"}</p>
              <p>{turn.summary}</p>
              {turn.escalation && <p className="warn">Escalation: {JSON.stringify(turn.escalation)}</p>}
            </div>
          )}
        </div>
      </div>

      {draft && (
        <div className="card review">
          <h3>Review before sending — {draft.review.lane === "issuer" ? "Issuer dispute lane" : "Merchant lane"}</h3>
          <dl>
            <dt>Exact destination</dt><dd><code>{draft.review.destination.address ?? draft.review.destination.recipient_ref}</code> <span className="muted">from {draft.review.destination.source}</span></dd>
            <dt>Amount</dt><dd>{money(draft.review.amount_minor, draft.review.currency)}</dd>
            <dt>Terms</dt><dd>{JSON.stringify(draft.review.terms)}</dd>
            <dt>Documents attached</dt><dd>{draft.review.documents.map((d) => <div key={d.document_id}><code>{d.document_id}</code> ({d.kind}) <span className="muted">{d.content_hash}</span></div>)}</dd>
            {draft.review.deadline && <><dt>Deadline (fixture)</dt><dd>{JSON.stringify(draft.review.deadline)}</dd></>}
            <dt className="warn">Irreversible effect</dt><dd className="warn">{draft.review.irreversible_effect}</dd>
          </dl>
          {draft.review.subject && (
            <details open>
              <summary>{draft.review.subject}</summary>
              <pre>{draft.review.body}</pre>
            </details>
          )}
          <p className="muted">payload {draft.payload_hash} · challenge {draft.approval_challenge_id} · expires {draft.challenge_expires_at} · case v{draft.expected_case_version}</p>
          <button
            disabled={busy}
            className="primary"
            onClick={() => run(() => api.approve(draft.action_id, { expected_case_version: draft.expected_case_version, action_payload_hash: draft.payload_hash, approval_challenge_id: draft.approval_challenge_id! }))}
          >
            Approve exactly this {draft.review.lane === "issuer" ? "dispute" : "message"}
          </button>
          <p className="muted">The worker executes the approved action; run it from the Operator tab.</p>
        </div>
      )}

      <div className="grid">
        <div className="card">
          <h3>Merchant lane</h3>
          {merchant ? <Lane channel={merchant} /> : <p className="muted">No merchant contact yet.</p>}
        </div>
        <div className="card">
          <h3>Issuer lane</h3>
          {issuer ? <Lane channel={issuer} /> : <p className="muted">No dispute opened. Requires separate approval after merchant follow-up.</p>}
        </div>
      </div>

      {timeline && (
        <div className="card">
          <h3>Timeline</h3>
          <table>
            <tbody>
              {timeline.events.map((e) => (
                <tr key={e.sequence}>
                  <td className="muted">{e.sequence}</td>
                  <td>{e.occurred_at.replace("T", " ").slice(0, 16)}</td>
                  <td>{e.event_type}{e.next_state && <> <span className="muted">{e.previous_state}</span> → <strong>{e.next_state}</strong></>}</td>
                  <td className="muted">{e.actor}</td>
                  <td className="muted small">{summarize(e.data)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <h4>Credit matches</h4>
          <table>
            <thead><tr><th>Transaction</th><th>Amount</th><th>Kind</th><th>Channel</th><th>Method</th><th>Confidence</th><th>Reversed</th></tr></thead>
            <tbody>
              {timeline.credit_matches.map((m) => (
                <tr key={m.id}><td><code>{m.transaction_id}</code></td><td>{money(m.amount_minor, m.currency)}</td><td>{m.credit_kind}</td><td>{m.channel_type}</td><td>{m.matching_method}</td><td>{m.confidence}</td><td>{m.reversed_at ?? ""}</td></tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function Lane({ channel }: { channel: CaseStatus["channels"][number] }) {
  return (
    <dl>
      <dt>Status</dt><dd>{channel.status}</dd>
      <dt>Provider reference</dt><dd><code>{channel.provider_case_ref ?? "—"}</code> <span className="muted">({channel.provider})</span></dd>
      <dt>Provider says</dt><dd>{channel.last_provider_status ?? "—"} <span className="muted">(simulated claim, not money movement)</span></dd>
      <dt>Follow-ups</dt><dd>{channel.followup_count}</dd>
      {channel.deadline_at && <><dt>Deadline</dt><dd>{channel.deadline_at.slice(0, 10)} <span className="muted">{channel.deadline_source}</span></dd></>}
    </dl>
  );
}

function PromiseForm({ caseId, max, onDone }: { caseId: string; max: number; onDone: () => Promise<void> }) {
  const [minor, setMinor] = useState(max);
  const [by, setBy] = useState("merchant email");
  const [date, setDate] = useState("2026-09-01T10:00:00Z");
  return (
    <form
      onSubmit={(e) => {
        e.preventDefault();
        api.answer(caseId, { promised_minor: minor, promised_by: by, promised_at: date }).then(onDone);
      }}
    >
      <label>Promised amount (cents) <input type="number" value={minor} onChange={(e) => setMinor(Number(e.target.value))} /></label>
      <label>Promised by <input value={by} onChange={(e) => setBy(e.target.value)} /></label>
      <label>Promised at <input value={date} onChange={(e) => setDate(e.target.value)} /></label>
      <button type="submit">Record promise</button>
    </form>
  );
}

function summarize(data: Record<string, any>): string {
  const keep = ["amount_minor", "credit_kind", "provider_ref", "refund_ref", "reason", "summary", "action_id", "type", "code"];
  return Object.entries(data).filter(([k]) => keep.includes(k)).map(([k, v]) => `${k}=${String(v)}`).join(" ");
}
