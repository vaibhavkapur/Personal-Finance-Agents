import { useState } from "react";
import { api } from "../api";
import type { CaseView } from "../types";

interface Props {
  token: string;
  view: CaseView;
  refresh: () => Promise<void>;
  onError: (e: unknown) => void;
}

type Answer = boolean | string | "unknown";

export function QuestionsPanel({ token, view, refresh, onError }: Props) {
  const [values, setValues] = useState<Record<string, Answer>>({});
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const answers = Object.entries(values).map(([question_id, value]) => ({ question_id, value }));
    if (answers.length === 0) return;
    setBusy(true);
    try {
      await api.answers(token, view.id, answers);
      setValues({});
      await refresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  const blocking = view.outstanding_questions.filter((q) => q.reason === "blocks_quote");
  const forApplication = view.outstanding_questions.filter((q) => q.reason === "required_for_application");

  const renderQuestion = (q: (typeof view.outstanding_questions)[number]) => (
    <tr key={q.question_id}>
      <td>{q.insurer_name ?? q.insurer_id}</td>
      <td>
        <em>"{q.text}"</em>
        <div className="muted">{q.question_id}</div>
      </td>
      <td>
        {q.type === "choice" ? (
          <select value={String(values[q.question_id] ?? "")} onChange={(e) => setValues({ ...values, [q.question_id]: e.target.value })}>
            <option value="">—</option>
            {(q.choices ?? []).map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
            <option value="unknown">I don't know</option>
          </select>
        ) : (
          <select
            value={values[q.question_id] === undefined ? "" : String(values[q.question_id])}
            onChange={(e) => setValues({ ...values, [q.question_id]: e.target.value === "true" ? true : e.target.value === "false" ? false : "unknown" })}
          >
            <option value="">—</option>
            <option value="true">Yes</option>
            <option value="false">No</option>
            <option value="unknown">I don't know</option>
          </select>
        )}
      </td>
    </tr>
  );

  return (
    <div className="panel">
      <h2>Insurer questions</h2>
      <p className="muted">Each question is shown in the insurer's own wording. Answers are never guessed or copied between insurers; "I don't know" is always allowed.</p>
      {blocking.length > 0 && (
        <>
          <h3>Needed before the insurer can quote</h3>
          <table>
            <tbody>{blocking.map(renderQuestion)}</tbody>
          </table>
        </>
      )}
      {forApplication.length > 0 && (
        <>
          <h3>Needed before an application can be prepared</h3>
          <table>
            <tbody>{forApplication.map(renderQuestion)}</tbody>
          </table>
        </>
      )}
      <div style={{ marginTop: 10 }}>
        <button className="primary" onClick={submit} disabled={busy || Object.keys(values).length === 0}>
          Confirm answers
        </button>
      </div>
    </div>
  );
}
