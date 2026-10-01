import { useEffect, useState } from "react";
import { InboxPage } from "./pages/InboxPage";
import { CasePage } from "./pages/CasePage";
import { OperatorPage } from "./pages/OperatorPage";

type Route = { name: "inbox" } | { name: "case"; id: string } | { name: "operator" };

function parseRoute(hash: string): Route {
  const h = hash.replace(/^#\/?/, "");
  if (h.startsWith("case/")) return { name: "case", id: h.slice(5) };
  if (h === "operator") return { name: "operator" };
  return { name: "inbox" };
}

export function navigate(path: string) {
  window.location.hash = path;
}

export function App() {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.hash));
  useEffect(() => {
    const onChange = () => setRoute(parseRoute(window.location.hash));
    window.addEventListener("hashchange", onChange);
    return () => window.removeEventListener("hashchange", onChange);
  }, []);

  return (
    <>
      <header className="top">
        <div className="row">
          <h1>Personal Banker</h1>
          <span className="env">mock environment · synthetic data · no real money</span>
        </div>
        <nav>
          <a href="#/inbox" className={route.name !== "operator" ? "active" : ""}>Customer</a>
          <a href="#/operator" className={route.name === "operator" ? "active" : ""}>Operator</a>
        </nav>
      </header>
      <main>
        {route.name === "inbox" && <InboxPage />}
        {route.name === "case" && <CasePage id={route.id} />}
        {route.name === "operator" && <OperatorPage />}
      </main>
    </>
  );
}
