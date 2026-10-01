import { useEffect, useState } from "react";
import { api } from "../api";
import type { Message } from "../types";

interface Props {
  token: string;
  caseId: string;
  refresh: () => Promise<void>;
  onError: (e: unknown) => void;
}

export function ChatPanel({ token, caseId, refresh, onError }: Props) {
  const [messages, setMessages] = useState<Message[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);

  const load = async () => {
    try {
      setMessages(await api.messages(token, caseId));
    } catch (e) {
      onError(e);
    }
  };

  useEffect(() => {
    void load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseId, token]);

  const send = async () => {
    if (!text.trim()) return;
    setBusy(true);
    try {
      await api.sendMessage(token, caseId, text.trim());
      setText("");
      await load();
      await refresh();
    } catch (e) {
      onError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="panel">
      <h2>Agent</h2>
      <p className="muted">The agent reads case state through typed tools. It cannot approve, submit or bind anything.</p>
      <div className="chat">
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            {m.content}
            {m.tool_calls && m.tool_calls.length > 0 && <span className="tools">tools: {m.tool_calls.map((t) => t.name).join(", ")}</span>}
          </div>
        ))}
        {messages.length === 0 && <div className="muted">Say what you need, e.g. "Find renters insurance that covers replacing my belongings and starts next month."</div>}
      </div>
      <textarea rows={3} value={text} onChange={(e) => setText(e.target.value)} placeholder="Message the agent… (tip: 'select <quote id>' prepares an application for review)" />
      <div style={{ marginTop: 6 }}>
        <button className="primary" onClick={send} disabled={busy}>
          Send
        </button>
      </div>
    </div>
  );
}
