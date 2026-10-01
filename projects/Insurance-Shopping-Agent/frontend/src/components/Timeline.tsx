import type { TimelineItem } from "../types";

const EMPHASIS: Record<string, string> = {
  quoted: "state",
  submitted: "state",
  bound: "state",
  issued: "state",
  "effective (coverage starts)": "ok",
  verified: "ok",
  completed: "ok",
  "revised offer": "warn",
  declined: "bad",
  "manual review": "bad",
};

export function Timeline({ items }: { items: TimelineItem[] }) {
  if (items.length === 0) return <div className="muted">No events yet.</div>;
  return (
    <ul className="timeline">
      {items.map((t, i) => (
        <li key={`${t.event_type}-${i}`}>
          <span className="when">{t.at.length > 10 ? t.at.slice(0, 16).replace("T", " ") : t.at}</span>
          <span>
            <span className={`badge ${EMPHASIS[t.label] ?? ""}`}>{t.label}</span> {t.state && <span className="muted">→ {t.state}</span>}
          </span>
        </li>
      ))}
    </ul>
  );
}
