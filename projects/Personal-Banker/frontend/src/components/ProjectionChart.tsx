import { money, Projection } from "../api";

export function ProjectionChart({ projection }: { projection: Projection }) {
  const days = projection.days ?? [];
  if (!days.length) return <p className="muted small">No day-level projection stored.</p>;
  const max = Math.max(...days.map((d) => d.closing_minor), projection.buffer_minor, 1);
  const bufferPct = (projection.buffer_minor / max) * 100;
  return (
    <div>
      <div className="chart" title="Projected available cash per day">
        <div className="buffer" style={{ bottom: `${bufferPct}%` }} />
        {days.map((d) => (
          <div
            key={d.date}
            className={`bar ${d.closing_minor < projection.buffer_minor ? "low" : d.events.length ? "event" : ""}`}
            style={{ height: `${Math.max((d.closing_minor / max) * 100, 1)}%` }}
            title={`${d.date}: ${money(d.closing_minor)}${d.events.length ? " — " + d.events.map((e) => `${e.label} ${money(e.amount_minor)}`).join(", ") : ""}`}
          />
        ))}
      </div>
      <div className="row small muted" style={{ justifyContent: "space-between", marginTop: 4 }}>
        <span>{days[0].date}</span>
        <span>buffer {money(projection.buffer_minor)} (dashed) · lowest from {projection.effective_date}: {money(projection.lowest_from_effective_minor)} on {projection.lowest_from_effective_date}</span>
        <span>{days[days.length - 1].date}</span>
      </div>
      <details>
        <summary>Dated events</summary>
        <table>
          <thead><tr><th>Date</th><th>Event</th><th className="num">Amount</th><th className="num">Closing</th><th>Source</th></tr></thead>
          <tbody>
            {days.filter((d) => d.events.length).flatMap((d) =>
              d.events.map((e, i) => (
                <tr key={`${d.date}-${i}`}>
                  <td>{d.date}</td>
                  <td>{e.label} {e.confirmed ? "" : <span className="pill warn">estimated</span>}</td>
                  <td className="num">{money(e.amount_minor)}</td>
                  <td className="num">{money(d.closing_minor)}</td>
                  <td className="mono muted">{e.source || e.ref}</td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </details>
    </div>
  );
}
