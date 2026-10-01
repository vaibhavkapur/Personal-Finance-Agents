import { useState } from "react";
import { api, money } from "../api";
import type { CaseView } from "../types";

interface Props {
  token: string;
  view: CaseView;
  refresh: () => Promise<void>;
  onError: (e: unknown) => void;
}

const ITEM_CLASSES = ["jewelry", "bicycles", "electronics", "musical_instruments", "home_business_property"];

export function InterviewPanel({ token, view, refresh, onError }: Props) {
  const n = view.needs;
  const [line1, setLine1] = useState(n.address?.line1 ?? "");
  const [city, setCity] = useState(n.address?.city ?? "");
  const [postal, setPostal] = useState(n.address?.postal_code ?? "");
  const [deductibleCap, setDeductibleCap] = useState<string>(n.deductible_cap_minor === null ? "" : String(n.deductible_cap_minor / 100));
  const [deductibleUnknown, setDeductibleUnknown] = useState(false);
  const [classes, setClasses] = useState<string[]>(n.required_item_classes ?? []);
  const [classesUnknown, setClassesUnknown] = useState(false);
  const [preference, setPreference] = useState(n.deductible_preference ?? "lower_premium");
  const [busy, setBusy] = useState(false);

  const save = async () => {
    const answers: unknown[] = [];
    if (line1 && city && postal) answers.push({ field: "address", value: { line1, city, state_code: "CA", postal_code: postal } });
    if (deductibleUnknown) answers.push({ field: "deductible_cap_minor", value: "unknown" });
    else if (deductibleCap !== "") answers.push({ field: "deductible_cap_minor", value: Math.round(Number(deductibleCap) * 100) });
    if (classesUnknown) answers.push({ field: "required_item_classes", value: "unknown" });
    else answers.push({ field: "required_item_classes", value: classes });
    answers.push({ field: "deductible_preference", value: preference });
    setBusy(true);
    try {
      await api.answers(token, view.id, answers);
      await refresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  const changesRequote = view.quotes.length > 0;

  return (
    <div className="panel">
      <h2>Needs interview</h2>
      <p className="muted">
        Confirmed requirements v{n.version}: property {money(n.property_limit_minor)}, liability {money(n.liability_limit_minor)}, replacement cost{" "}
        {n.replacement_cost_required === null ? "unknown" : n.replacement_cost_required ? "required" : "not required"}, start {n.effective_date ?? "unknown"}.
        {view.missing_fields.length > 0 && <> Missing: <strong>{view.missing_fields.join(", ")}</strong>.</>}
      </p>
      <div className="grid2">
        <div>
          <label>Street address</label>
          <input value={line1} onChange={(e) => setLine1(e.target.value)} placeholder="1200 Fixture Ave Apt 4B" />
        </div>
        <div className="grid2">
          <div>
            <label>City</label>
            <input value={city} onChange={(e) => setCity(e.target.value)} />
          </div>
          <div>
            <label>ZIP</label>
            <input value={postal} onChange={(e) => setPostal(e.target.value)} />
          </div>
        </div>
        <div>
          <label>Highest deductible you would accept ($)</label>
          <div className="row">
            <input type="number" value={deductibleCap} disabled={deductibleUnknown} onChange={(e) => setDeductibleCap(e.target.value)} />
            <label style={{ margin: 0, whiteSpace: "nowrap" }}>
              <input type="checkbox" style={{ width: "auto" }} checked={deductibleUnknown} onChange={(e) => setDeductibleUnknown(e.target.checked)} /> I don't know
            </label>
          </div>
        </div>
        <div>
          <label>Ranking preference (applied only after suitability)</label>
          <select value={preference} onChange={(e) => setPreference(e.target.value)}>
            <option value="lower_premium">Lower premium first</option>
            <option value="lower_deductible">Lower deductible first</option>
          </select>
        </div>
      </div>
      <label style={{ marginTop: 8 }}>Item classes that must be covered</label>
      <div className="row" style={{ flexWrap: "wrap" }}>
        {ITEM_CLASSES.map((c) => (
          <label key={c} style={{ margin: 0 }}>
            <input
              type="checkbox"
              style={{ width: "auto" }}
              disabled={classesUnknown}
              checked={classes.includes(c)}
              onChange={(e) => setClasses(e.target.checked ? [...classes, c] : classes.filter((x) => x !== c))}
            />{" "}
            {c.replace(/_/g, " ")}
          </label>
        ))}
        <label style={{ margin: 0 }}>
          <input type="checkbox" style={{ width: "auto" }} checked={classesUnknown} onChange={(e) => setClassesUnknown(e.target.checked)} /> I don't know
        </label>
      </div>
      <div style={{ marginTop: 10 }} className="row">
        <button className="primary" onClick={save} disabled={busy}>
          Save answers
        </button>
        {changesRequote && <span className="muted warn">Changing a material field re-quotes all insurers and invalidates any pending approval.</span>}
      </div>
    </div>
  );
}
