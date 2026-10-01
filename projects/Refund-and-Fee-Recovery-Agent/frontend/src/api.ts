import type { AgentTurn, CaseStatus, Draft, Timeline } from "./types";

export type Role = "customer" | "operator";

const TOKENS: Record<Role, string> = { customer: "tok_demo_customer", operator: "tok_demo_operator" };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) {
    super(message);
  }
}

async function call<T>(role: Role, method: string, path: string, body?: unknown, headers: Record<string, string> = {}): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${TOKENS[role]}`, ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  const json = text ? JSON.parse(text) : {};
  if (!res.ok) {
    const err = json.error || json.detail || {};
    throw new ApiError(res.status, err.code || "error", err.message || res.statusText);
  }
  return json as T;
}

export const api = {
  listCases: () => call<{ items: CaseStatus[] }>("customer", "GET", "/v1/recovery-cases"),
  getCase: (id: string) => call<CaseStatus>("customer", "GET", `/v1/recovery-cases/${id}`),
  createCase: (body: { order_ref: string; target_minor: number; currency: string; evidence_ids: string[] }) =>
    call<{ id: string; status: string; version: number; next_step: string }>("customer", "POST", "/v1/recovery-cases", { customer_id: "cus_demo_4", reason_code: "promised_refund_missing", ...body }),
  reconcile: (id: string) => call<any>("customer", "POST", `/v1/recovery-cases/${id}/reconcile`),
  timeline: (id: string) => call<Timeline>("customer", "GET", `/v1/recovery-cases/${id}/timeline`),
  agentTurn: (id: string, message?: string) => call<AgentTurn>("customer", "POST", `/v1/recovery-cases/${id}/agent-turns`, { message }),
  draftMessage: (id: string) => call<Draft>("customer", "POST", `/v1/recovery-cases/${id}/merchant-message-drafts`, undefined, { "Idempotency-Key": `ui-${id}-msg` }),
  draftDispute: (id: string) => call<Draft>("customer", "POST", `/v1/recovery-cases/${id}/dispute-drafts`, undefined, { "Idempotency-Key": `ui-${id}-dispute` }),
  getAction: (caseId: string, actionId: string) => call<Draft>("customer", "GET", `/v1/recovery-cases/${caseId}/actions/${actionId}`),
  approve: (actionId: string, body: { expected_case_version: number; action_payload_hash: string; approval_challenge_id: string }) =>
    call<any>("customer", "POST", `/v1/actions/${actionId}/approve`, body),
  answer: (id: string, answer: Record<string, unknown>) => call<any>("customer", "POST", `/v1/recovery-cases/${id}/answers`, { answer }),
  closeUnresolved: (id: string, reason: string) => call<any>("customer", "POST", `/v1/recovery-cases/${id}/close-unresolved`, { reason }),
  ops: {
    overview: () => call<any>("operator", "GET", "/v1/ops/overview"),
    runWorker: () => call<any>("operator", "POST", "/v1/ops/worker/run"),
    advance: (days: number) => call<any>("operator", "POST", "/v1/ops/clock/advance", { days }),
    replay: (provider: string, event_id: string) => call<any>("operator", "POST", "/v1/ops/events/replay", { provider, event_id }),
    release: (caseId: string, target_status: string, note: string) => call<any>("operator", "POST", `/v1/ops/cases/${caseId}/release`, { target_status, note }),
    toolRuns: () => call<any>("operator", "GET", "/v1/ops/tool-runs"),
  },
};

export function money(minor: number, currency: string): string {
  return `${(minor / 100).toFixed(2)} ${currency}`;
}
