import { useState } from "react";
import { api } from "../api";

interface Props {
  token: string;
  onCreated: (id: string) => void | Promise<void>;
  onError: (e: unknown) => void;
}

export function NewCaseForm({ token, onCreated, onError }: Props) {
  const [effective, setEffective] = useState("2026-11-01");
  const [property, setProperty] = useState(30000);
  const [liability, setLiability] = useState(100000);
  const [replacement, setReplacement] = useState<"yes" | "no" | "unknown">("yes");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    try {
      const created = await api.createCase(token, {
        state_code: "CA",
        product: "renters",
        desired_effective_date: effective,
        property_limit_minor: property * 100,
        liability_limit_minor: liability * 100,
        replacement_cost_required: replacement === "unknown" ? null : replacement === "yes",
      });
      await onCreated(created.id);
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>New renters case (CA)</h2>
      <label>Coverage start date</label>
      <input type="date" value={effective} onChange={(e) => setEffective(e.target.value)} />
      <div className="grid2" style={{ marginTop: 8 }}>
        <div>
          <label>Personal property ($)</label>
          <input type="number" value={property} onChange={(e) => setProperty(Number(e.target.value))} />
        </div>
        <div>
          <label>Liability ($)</label>
          <input type="number" value={liability} onChange={(e) => setLiability(Number(e.target.value))} />
        </div>
      </div>
      <label style={{ marginTop: 8 }}>Replacement-cost settlement required?</label>
      <select value={replacement} onChange={(e) => setReplacement(e.target.value as "yes" | "no" | "unknown")}>
        <option value="yes">Yes</option>
        <option value="no">No</option>
        <option value="unknown">I don't know</option>
      </select>
      <div style={{ marginTop: 10 }}>
        <button className="primary" onClick={submit} disabled={busy}>
          Start case
        </button>
      </div>
    </div>
  );
}
