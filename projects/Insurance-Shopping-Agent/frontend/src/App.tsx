import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "./api";
import type { CaseListItem, CaseView } from "./types";
import { CaseDetail } from "./components/CaseDetail";
import { ChatPanel } from "./components/ChatPanel";
import { NewCaseForm } from "./components/NewCaseForm";
import { OperatorView } from "./components/OperatorView";

const SESSIONS = [
  { label: "Avery Demo (customer)", token: "tok_cus_demo_1", kind: "customer" },
  { label: "Jordan Sample (customer)", token: "tok_cus_demo_2", kind: "customer" },
  { label: "Riley Fixture (customer)", token: "tok_cus_demo_3", kind: "customer" },
  { label: "Operator", token: "tok_operator", kind: "operator" },
] as const;

export function App() {
  const [token, setToken] = useState<string>(SESSIONS[0].token);
  const session = SESSIONS.find((s) => s.token === token) ?? SESSIONS[0];
  const [cases, setCases] = useState<CaseListItem[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<CaseView | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [env, setEnv] = useState<{ environment: string; adapter_mode: string; now: string } | null>(null);

  const refreshList = useCallback(async () => {
    if (session.kind !== "customer") return;
    try {
      setCases(await api.listCases(token));
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token, session.kind]);

  const refreshCase = useCallback(async () => {
    if (!selected || session.kind !== "customer") return;
    try {
      setView(await api.getCase(token, selected));
      setCases(await api.listCases(token));
      setError(null);
    } catch (e) {
      setError((e as Error).message);
    }
  }, [token, selected, session.kind]);

  useEffect(() => {
    setSelected(null);
    setView(null);
    void refreshList();
    api.catalog(token).then((c) => setEnv({ environment: c.environment, adapter_mode: c.adapter_mode, now: c.now })).catch(() => setEnv(null));
  }, [token, refreshList]);

  useEffect(() => {
    void refreshCase();
  }, [refreshCase]);

  const onError = useCallback((e: unknown) => {
    if (e instanceof ApiError) setError(`${e.status}: ${e.message}${e.details ? " " + JSON.stringify(e.details) : ""}`);
    else setError((e as Error).message);
  }, []);

  return (
    <>
      <header className="topbar">
        <strong>Insurance Shopping Agent</strong>
        <span className="badge mock">prototype · fictional insurers · {env ? `${env.environment} / ${env.adapter_mode}` : "…"}</span>
        {env && <span className="badge">fixture clock {env.now.slice(0, 16).replace("T", " ")}</span>}
        <span className="spacer" />
        <label style={{ margin: 0 }}>
          Session&nbsp;
          <select value={token} onChange={(e) => setToken(e.target.value)} style={{ width: "auto", display: "inline-block" }}>
            {SESSIONS.map((s) => (
              <option key={s.token} value={s.token}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </header>
      {error && <div className="error" style={{ padding: "6px 20px" }}>{error}</div>}
      {session.kind === "operator" ? (
        <OperatorView token={token} onError={onError} />
      ) : (
        <div className="layout">
          <aside>
            <div className="panel">
              <h2>My cases</h2>
              <ul className="list">
                {cases.map((c) => (
                  <li key={c.id} className={c.id === selected ? "active" : ""} onClick={() => setSelected(c.id)}>
                    <div>
                      <span className="badge state">{c.status}</span>
                    </div>
                    <div className="muted">{c.id}</div>
                  </li>
                ))}
                {cases.length === 0 && <li className="muted">No cases yet.</li>}
              </ul>
            </div>
            <NewCaseForm
              token={token}
              onCreated={async (id) => {
                await refreshList();
                setSelected(id);
              }}
              onError={onError}
            />
          </aside>
          <main>
            {view ? (
              <CaseDetail token={token} view={view} refresh={refreshCase} onError={onError} />
            ) : (
              <div className="panel muted">Select or create a case to start the needs interview.</div>
            )}
          </main>
          <aside>{view && <ChatPanel token={token} caseId={view.id} refresh={refreshCase} onError={onError} />}</aside>
        </div>
      )}
    </>
  );
}
