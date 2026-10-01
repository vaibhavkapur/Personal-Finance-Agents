import { useEffect, useState } from "react";
import { api, money } from "../api";
import type { CaseView, Comparison } from "../types";
import { InterviewPanel } from "./InterviewPanel";
import { QuestionsPanel } from "./QuestionsPanel";
import { ComparisonPanel } from "./ComparisonPanel";
import { ReviewModal } from "./ReviewModal";
import { Timeline } from "./Timeline";

interface Props {
  token: string;
  view: CaseView;
  refresh: () => Promise<void>;
  onError: (e: unknown) => void;
}

export function CaseDetail({ token, view, refresh, onError }: Props) {
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [showReview, setShowReview] = useState(false);
  const [busy, setBusy] = useState(false);

  const canCompare = view.quotes.length > 0 || view.quote_tasks.length > 0;

  useEffect(() => {
    if (!canCompare) {
      setComparison(null);
      return;
    }
    api.comparison(token, view.id).then(setComparison).catch(onError);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [view.id, view.version, canCompare]);

  const run = async (fn: () => Promise<unknown>) => {
    setBusy(true);
    try {
      await fn();
      await refresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  const pending = view.pending_action;
  const preSubmission = ["collecting", "quoting", "needs_information", "comparing", "awaiting_selection", "awaiting_approval", "expired", "declined"].includes(view.status);

  return (
    <>
      <div className="panel">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <h2 style={{ margin: 0 }}>
            Case {view.id} <span className="badge state">{view.status}</span> <span className="badge">v{view.version}</span>
          </h2>
          <span className="muted">
            {view.environment} · {view.adapter_mode}
          </span>
        </div>
        <div className="next" style={{ marginTop: 10 }}>
          <strong>Next decision:</strong> {view.next_decision}
          {view.review_reason && <div className="bad">Review reason: {view.review_reason}</div>}
        </div>
        <div className="row">
          {view.status === "collecting" && (
            <button className="primary" disabled={busy || view.missing_fields.filter((f) => f !== "deductible_preference").length > 0} onClick={() => run(() => api.requestQuotes(token, view.id))}>
              Request quotes from 3 insurers
            </button>
          )}
          {view.status === "expired" && (
            <button className="primary" disabled={busy} onClick={() => run(() => api.requestQuotes(token, view.id))}>
              Request fresh quotes
            </button>
          )}
          {pending && pending.status === "proposed" && (
            <button className="primary" onClick={() => setShowReview(true)}>
              Review {pending.type === "accept_revised_offer" ? "revised offer" : "application"}
            </button>
          )}
          <button onClick={() => void refresh()} disabled={busy}>
            Refresh
          </button>
        </div>
      </div>

      {view.policy && (
        <div className="panel">
          <h2>Policy</h2>
          <div className="row">
            <span className={`badge ${view.policy.verified ? "ok" : "bad"}`}>{view.policy.verified ? "verified against approved terms" : "verification failed"}</span>
            <span className="badge">{view.policy.coverage_label}</span>
          </div>
          <p>
            Insurer policy reference <strong>{view.policy.insurer_policy_ref}</strong>. Effective <strong>{view.policy.effective_at}</strong> to {view.policy.expires_at}.
            {view.policy.coverage_starts_in_future && <span className="warn"> Coverage is not in force before the effective date.</span>}
          </p>
          {!view.policy.verified && (
            <ul>
              {view.policy.verification.mismatches.map((m) => (
                <li key={m.field} className="bad">
                  {m.field}: {m.detail}
                </li>
              ))}
            </ul>
          )}
          <details>
            <summary className="muted">Declarations (simulated document {String(view.policy.declarations["document_id"] ?? "")})</summary>
            <pre>{JSON.stringify(view.policy.declarations, null, 2)}</pre>
          </details>
        </div>
      )}

      {preSubmission && <InterviewPanel token={token} view={view} refresh={refresh} onError={onError} />}

      {view.outstanding_questions.length > 0 && <QuestionsPanel token={token} view={view} refresh={refresh} onError={onError} />}

      {view.quote_tasks.length > 0 && (
        <div className="panel">
          <h2>Insurer tasks</h2>
          <table>
            <thead>
              <tr>
                <th>Insurer</th>
                <th>Status</th>
                <th>External task</th>
                <th>Note</th>
              </tr>
            </thead>
            <tbody>
              {view.quote_tasks.map((t) => (
                <tr key={t.task_id}>
                  <td>{t.insurer_name}</td>
                  <td>
                    <span className={`badge ${t.status === "quoted" ? "ok" : t.status === "timeout" || t.status === "failed" ? "bad" : "warn"}`}>{t.status}</span>
                  </td>
                  <td className="muted">{t.external_task_id ?? "—"}</td>
                  <td className="muted">{t.last_error ?? ""}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {comparison && (
        <ComparisonPanel
          comparison={comparison}
          view={view}
          onSelect={(quoteId) => run(() => api.prepare(token, view.id, quoteId))}
          busy={busy || !["awaiting_selection", "comparing", "awaiting_approval"].includes(view.status)}
        />
      )}

      <div className="panel">
        <h2>Timeline</h2>
        <Timeline items={view.timeline} />
      </div>

      {view.actions.length > 0 && (
        <div className="panel">
          <h2>Actions and approvals</h2>
          <table>
            <thead>
              <tr>
                <th>Action</th>
                <th>Status</th>
                <th>Amount</th>
                <th>Payload hash</th>
              </tr>
            </thead>
            <tbody>
              {view.actions.map((a) => (
                <tr key={a.action_id}>
                  <td>{a.type}</td>
                  <td>
                    <span className="badge">{a.status}</span>
                  </td>
                  <td>{a.review?.amount.display ?? money(null)}</td>
                  <td className="hash">{a.payload_hash}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showReview && pending && pending.review && (
        <ReviewModal
          action={pending}
          caseVersion={view.version}
          onClose={() => setShowReview(false)}
          onApprove={() =>
            run(async () => {
              await api.approve(token, pending.action_id, {
                expected_case_version: view.version,
                action_payload_hash: pending.payload_hash,
                approval_challenge_id: pending.challenge_id ?? "",
              });
              setShowReview(false);
            })
          }
          onReject={(reason) =>
            run(async () => {
              await api.reject(token, pending.action_id, reason);
              setShowReview(false);
            })
          }
        />
      )}
    </>
  );
}
