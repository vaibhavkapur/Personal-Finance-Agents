import { useState } from "react";
import { CaseView } from "./views/CaseView";
import { Inbox } from "./views/Inbox";
import { Operator } from "./views/Operator";

type Tab = "inbox" | "case" | "operator";

export function App() {
  const [tab, setTab] = useState<Tab>("inbox");
  const [caseId, setCaseId] = useState<string | null>(null);

  const openCase = (id: string) => {
    setCaseId(id);
    setTab("case");
  };

  return (
    <div className="app">
      <header>
        <h1>Refund &amp; Fee-Recovery Agent</h1>
        <span className="badge env">mock environment · synthetic fixtures · no real refunds or disputes are initiated</span>
        <nav>
          <button className={tab === "inbox" ? "active" : ""} onClick={() => setTab("inbox")}>Recovery inbox</button>
          <button className={tab === "case" ? "active" : ""} onClick={() => setTab("case")} disabled={!caseId}>Case</button>
          <button className={tab === "operator" ? "active" : ""} onClick={() => setTab("operator")}>Operator</button>
        </nav>
      </header>
      <main>
        {tab === "inbox" && <Inbox onOpen={openCase} />}
        {tab === "case" && caseId && <CaseView caseId={caseId} />}
        {tab === "operator" && <Operator />}
      </main>
    </div>
  );
}
