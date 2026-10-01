import { useEffect, useState } from "react";
import { ApiError, api, money } from "../api";
import type { CaseStatus } from "../types";

const FIXTURE_ORDERS = [
  { order_ref: "order_mock_499", target_minor: 8499, evidence_ids: ["receipt_mock_1", "promise_mock_1"], label: "StreamBox annual — $84.99 promised, missing (Demo 1)" },
  { order_ref: "order_mock_500", target_minor: 8499, evidence_ids: ["receipt_mock_2", "promise_mock_2"], label: "StreamBox annual — partial $50 arrives (Demo 2)" },
  { order_ref: "order_mock_503", target_minor: 12000, evidence_ids: ["receipt_mock_5", "promise_mock_5"], label: "StreamBox premium — merchant claims refund, issuer lane (Demo 3)" },
  { order_ref: "order_mock_501", target_minor: 5900, evidence_ids: ["receipt_mock_3", "promise_mock_3"], label: "StreamBox add-on — already refunded" },
  { order_ref: "order_mock_502", target_minor: 8499, evidence_ids: ["receipt_mock_4", "promise_mock_4"], label: "StreamBox annual — store credit instead" },
  { order_ref: "order_mock_504", target_minor: 4500, evidence_ids: ["receipt_mock_6", "promise_mock_6"], label: "StreamBox monthly — ambiguous credits" },
  { order_ref: "order_mock_505", target_minor: 3000, evidence_ids: ["receipt_mock_7"], label: "StreamBox monthly — no verified promise" },
];

export function Inbox({ onOpen }: { onOpen: (id: string) => void }) {
  const [items, setItems] = useState<CaseStatus[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = () => api.listCases().then((r) => setItems(r.items)).catch((e: ApiError) => setError(e.message));
  useEffect(() => {
    load();
  }, []);

  const create = async (o: (typeof FIXTURE_ORDERS)[number]) => {
    setBusy(true);
    setError(null);
    try {
      const created = await api.createCase({ order_ref: o.order_ref, target_minor: o.target_minor, currency: "USD", evidence_ids: o.evidence_ids });
      await load();
      onOpen(created.id);
    } catch (e) {
      setError((e as ApiError).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <h2>Recovery inbox</h2>
      <p className="muted">Each row shows the original charge, the verified promise and what is still missing. Nothing here contacts a merchant.</p>
      {error && <div className="error">{error}</div>}
      <table>
        <thead>
          <tr><th>Order</th><th>Charged</th><th>Promised</th><th>Final posted</th><th>Missing</th><th>Provisional</th><th>Store credit</th><th>State</th><th>Next decision</th></tr>
        </thead>
        <tbody>
          {items.map((c) => (
            <tr key={c.case_id} onClick={() => onOpen(c.case_id)} className="clickable">
              <td>{c.order_ref}</td>
              <td>{money(c.amounts.requested_minor, c.amounts.currency)}</td>
              <td>{money(c.amounts.promised_minor, c.amounts.currency)}</td>
              <td>{money(c.amounts.final_recovered_minor, c.amounts.currency)}</td>
              <td className={c.amounts.outstanding_minor > 0 ? "warn" : "ok"}>{money(c.amounts.outstanding_minor, c.amounts.currency)}</td>
              <td>{money(c.amounts.provisional_minor, c.amounts.currency)}</td>
              <td>{money(c.amounts.store_credit_minor, c.amounts.currency)}</td>
              <td><span className={`state ${c.status}`}>{c.status}</span></td>
              <td>{c.pending_question ? <strong>Answer question: {c.pending_question.kind}</strong> : c.next_step}</td>
            </tr>
          ))}
          {items.length === 0 && <tr><td colSpan={9} className="muted">No cases yet. Open one from a fixture order below.</td></tr>}
        </tbody>
      </table>
      <h3>Open a case from a synthetic order</h3>
      <ul className="fixtures">
        {FIXTURE_ORDERS.map((o) => (
          <li key={o.order_ref}>
            <button disabled={busy} onClick={() => create(o)}>Open</button> <code>{o.order_ref}</code> — {o.label}
          </li>
        ))}
      </ul>
    </section>
  );
}
