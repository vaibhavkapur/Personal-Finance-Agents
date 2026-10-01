import { useState } from "react";
import type { Action } from "../types";

interface Props {
  action: Action;
  caseVersion: number;
  onClose: () => void;
  onApprove: () => void;
  onReject: (reason: string) => void;
}

export function ReviewModal({ action, caseVersion, onClose, onApprove, onReject }: Props) {
  const review = action.review!;
  const [confirmed, setConfirmed] = useState(false);
  const revised = action.type === "accept_revised_offer";
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <h2>{revised ? "Review revised terms" : "Review application before submission"}</h2>
        <p className="muted">
          Nothing has been sent. Approving authorises exactly the payload below (hash-bound) for case version {caseVersion}. The challenge expires at {review.expires_at.slice(0, 16).replace("T", " ")}.
        </p>
        {revised && (
          <div className="next">
            Underwriting changed the premium from <strong>{review.previous_premium_display}</strong> to <strong>{review.amount.display}</strong> ({review.premium_change_display}).
            <div className="muted">Reason given by the insurer: {review.revision_reason}</div>
          </div>
        )}
        <h3>Destination</h3>
        <p>
          {review.destination.insurer_name} <span className="badge mock">{review.destination.environment}</span> <span className="badge">{review.destination.protocol}</span>
        </p>
        <h3>Amount</h3>
        <p>
          <strong>{review.amount.display}</strong>
        </p>
        <h3>Terms</h3>
        <table>
          <tbody>
            <tr>
              <th>Personal property limit</th>
              <td>{review.terms.property_limit}</td>
            </tr>
            <tr>
              <th>Liability limit</th>
              <td>{review.terms.liability_limit}</td>
            </tr>
            <tr>
              <th>Deductible</th>
              <td>{review.terms.deductible}</td>
            </tr>
            <tr>
              <th>Replacement cost</th>
              <td>{review.terms.replacement_cost ? "yes" : "no"}</td>
            </tr>
            <tr>
              <th>Coverage start (effective date)</th>
              <td>{review.terms.effective_date}</td>
            </tr>
            <tr>
              <th>Policy form</th>
              <td>{review.terms.policy_form_version}</td>
            </tr>
            <tr>
              <th>Exclusions</th>
              <td>{review.terms.exclusions.join(", ")}</td>
            </tr>
            <tr>
              <th>Endorsements</th>
              <td>{review.terms.endorsements.join(", ") || "none"}</td>
            </tr>
          </tbody>
        </table>
        <h3>Answers that will be sent</h3>
        <table>
          <tbody>
            {review.answers.map((a) => (
              <tr key={a.question_id}>
                <th>{a.question_text}</th>
                <td>{String(a.value)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <h3>Documents disclosed</h3>
        <p className="muted">{review.documents_disclosed.length === 0 ? "None. Quote consent is separate from document disclosure." : review.documents_disclosed.join(", ")}</p>
        <h3>Irreversible effects</h3>
        <ul className="effects">
          {review.irreversible_effects.map((e) => (
            <li key={e}>{e}</li>
          ))}
        </ul>
        <div className="hash">action payload hash {action.payload_hash}</div>
        <label style={{ marginTop: 12 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} /> I reviewed the destination, amount, terms and answers above.
        </label>
        <div className="row" style={{ marginTop: 12, justifyContent: "flex-end" }}>
          <button onClick={onClose}>Close</button>
          <button className="danger" onClick={() => onReject("rejected by customer on review screen")}>
            Reject
          </button>
          <button className="primary" disabled={!confirmed} onClick={onApprove}>
            Approve {revised ? "revised terms" : "and submit"}
          </button>
        </div>
      </div>
    </div>
  );
}
