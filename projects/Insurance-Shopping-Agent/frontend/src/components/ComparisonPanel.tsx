import { money } from "../api";
import type { CaseView, Check, Comparison, ComparisonEntry } from "../types";

interface Props {
  comparison: Comparison;
  view: CaseView;
  onSelect: (quoteId: string) => void;
  busy: boolean;
}

function Cite({ check }: { check: Check }) {
  if (!check.citation) return null;
  return (
    <div className="cite">
      <code>{check.citation.clause_id}</code> {check.citation.title} ({check.citation.policy_form_version})
      {check.citation.text && <div>“{check.citation.text}”</div>}
    </div>
  );
}

function QuoteCard({ entry, kind, onSelect, busy }: { entry: ComparisonEntry; kind: "suitable" | "excluded" | "undetermined"; onSelect?: (id: string) => void; busy: boolean }) {
  const failing = entry.failed_checks ?? entry.unknown_checks ?? [];
  return (
    <div className={`quote ${kind}`}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div>
          {entry.rank && <span className="badge state">#{entry.rank}</span>} <strong>{entry.insurer_name}</strong> <span className="muted">form {entry.policy_form_version}</span>
        </div>
        <div>
          <strong>{entry.annual_premium_display}</strong>/year · deductible {money(entry.deductible_minor)} · valid until {entry.valid_until.slice(0, 10)}
        </div>
      </div>
      {kind !== "suitable" && (
        <div style={{ marginTop: 6 }}>
          <span className={kind === "excluded" ? "bad" : "warn"}>{kind === "excluded" ? "Excluded: " : "Cannot be shortlisted: "}</span>
          {entry.reason}
          {failing.map((c) => (
            <Cite key={c.field} check={c} />
          ))}
        </div>
      )}
      {kind === "suitable" && onSelect && (
        <div style={{ marginTop: 8 }} className="row">
          <button className="primary" disabled={busy} onClick={() => onSelect(entry.quote_id)}>
            Select and prepare application
          </button>
          <span className="muted">quote id {entry.quote_id}</span>
        </div>
      )}
      <details style={{ marginTop: 6 }}>
        <summary className="muted">Requirement checks ({entry.checks.filter((c) => c.result === "pass").length}/{entry.checks.length} pass)</summary>
        <table>
          <tbody>
            {entry.checks.map((c) => (
              <tr key={c.field}>
                <td className={c.result === "pass" ? "ok" : c.result === "fail" ? "bad" : "warn"}>{c.result}</td>
                <td>{c.field}</td>
                <td>
                  {c.explanation}
                  <Cite check={c} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </details>
    </div>
  );
}

export function ComparisonPanel({ comparison, view, onSelect, busy }: Props) {
  const quoteRefs = Object.keys(comparison.differences[0]?.values ?? {});
  const insurerOf = (ref: string) => comparison.differences[0]?.values[ref]?.insurer_id ?? ref;
  const nameOf = (insurerId: string) => view.quotes.find((q) => q.insurer_id === insurerId)?.insurer_name ?? insurerId;

  return (
    <div className="panel">
      <h2>Coverage comparison</h2>
      {!comparison.complete && (
        <div className="warn" style={{ marginBottom: 8 }}>
          {comparison.missing_responses.map((m) => (
            <div key={m.insurer_id}>⚠ {m.disclosure}</div>
          ))}
        </div>
      )}
      {comparison.unknown_needs_fields.length > 0 && <div className="warn">Unknown requirements: {comparison.unknown_needs_fields.join(", ")} — quotes cannot be shortlisted until confirmed.</div>}

      <h3>Meets all your requirements</h3>
      {comparison.suitable.length === 0 && <div className="muted">None yet.</div>}
      {comparison.suitable.map((e) => (
        <QuoteCard key={e.quote_id} entry={e} kind="suitable" onSelect={onSelect} busy={busy} />
      ))}
      {comparison.undetermined.map((e) => (
        <QuoteCard key={e.quote_id} entry={e} kind="undetermined" busy={busy} />
      ))}
      {comparison.excluded.length > 0 && <h3>Excluded (fails a hard requirement)</h3>}
      {comparison.excluded.map((e) => (
        <QuoteCard key={e.quote_id} entry={e} kind="excluded" busy={busy} />
      ))}

      {comparison.trade_offs.length > 0 && (
        <>
          <h3>Trade-offs</h3>
          <ul>
            {comparison.trade_offs.map((t) => (
              <li key={t}>{t}</li>
            ))}
          </ul>
        </>
      )}

      {quoteRefs.length > 0 && (
        <>
          <h3>Field-by-field differences</h3>
          <table>
            <thead>
              <tr>
                <th>Field</th>
                {quoteRefs.map((ref) => (
                  <th key={ref}>{nameOf(insurerOf(ref))}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {comparison.differences.map((row) => (
                <tr key={row.field} className={row.differs ? "differs" : ""}>
                  <td>
                    {row.label}
                    {row.note && <div className="muted">{row.note}</div>}
                  </td>
                  {quoteRefs.map((ref) => {
                    const v = row.values[ref];
                    return (
                      <td key={ref}>
                        {v ? v.display : "—"}
                        {v?.citation && (
                          <div className="cite" title={v.citation.text ?? ""}>
                            <code>{v.citation.clause_id}</code>
                          </div>
                        )}
                      </td>
                    );
                  })}
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
      <p className="muted" style={{ marginTop: 10 }}>
        Ranking: {comparison.ranking.rule} {comparison.ranking.disclosure}
      </p>
    </div>
  );
}
