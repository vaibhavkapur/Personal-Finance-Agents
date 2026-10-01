const TONE: Record<string, string> = {
  completed: "ok",
  submitted: "info",
  verifying: "info",
  approved: "info",
  awaiting_approval: "warn",
  needs_information: "warn",
  needs_requote: "warn",
  outcome_unknown: "warn",
  manual_review: "bad",
  rejected: "bad",
  cancelled: "",
};

export function StatePill({ state }: { state: string }) {
  return <span className={`pill ${TONE[state] ?? ""}`}>{state.replaceAll("_", " ")}</span>;
}

export const STEPS = ["collecting", "evaluating", "awaiting_approval", "approved", "submitted", "verifying", "completed"];

export function Stepper({ state }: { state: string }) {
  const idx = STEPS.indexOf(state);
  const side = idx === -1;
  return (
    <div className="steps">
      {STEPS.map((s, i) => (
        <span key={s} className={!side && i < idx ? "done" : !side && i === idx ? "now" : ""}>{s.replaceAll("_", " ")}</span>
      ))}
      {side && <span className="now">{state.replaceAll("_", " ")}</span>}
    </div>
  );
}
