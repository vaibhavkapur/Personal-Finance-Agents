import type { CaseListItem, CaseView, Comparison, Message, OperatorCase } from "./types";

const BASE = import.meta.env.VITE_API_BASE ?? "";

export class ApiError extends Error {
  status: number;
  details: unknown;
  constructor(status: number, message: string, details?: unknown) {
    super(message);
    this.status = status;
    this.details = details;
  }
}

async function request<T>(token: string, method: string, path: string, body?: unknown): Promise<T> {
  const response = await fetch(BASE + path, {
    method,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${token}` },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await response.text();
  const data = text ? JSON.parse(text) : null;
  if (!response.ok) {
    throw new ApiError(response.status, (data && data.detail) || response.statusText, data && data.details);
  }
  return data as T;
}

export const api = {
  me: (token: string) => request<{ customer_id: string; display_name: string; address: Record<string, string> }>(token, "GET", "/v1/me"),
  catalog: (token: string) => request<{ insurers: { insurer_id: string; display_name: string; label: string }[]; now: string; environment: string; adapter_mode: string }>(token, "GET", "/v1/catalog"),
  listCases: (token: string) => request<CaseListItem[]>(token, "GET", "/v1/insurance-shopping-cases"),
  createCase: (token: string, body: unknown) => request<{ id: string }>(token, "POST", "/v1/insurance-shopping-cases", body),
  getCase: (token: string, id: string) => request<CaseView>(token, "GET", `/v1/insurance-shopping-cases/${id}`),
  answers: (token: string, id: string, answers: unknown[]) => request<CaseView>(token, "POST", `/v1/insurance-shopping-cases/${id}/answers`, { answers }),
  requestQuotes: (token: string, id: string) => request<unknown>(token, "POST", `/v1/insurance-shopping-cases/${id}/quote-requests`),
  comparison: (token: string, id: string) => request<Comparison>(token, "GET", `/v1/insurance-shopping-cases/${id}/comparison`),
  prepare: (token: string, id: string, quoteId: string) => request<unknown>(token, "POST", `/v1/insurance-shopping-cases/${id}/applications`, { quote_id: quoteId }),
  approve: (token: string, actionId: string, body: { expected_case_version: number; action_payload_hash: string; approval_challenge_id: string }) =>
    request<unknown>(token, "POST", `/v1/actions/${actionId}/approve`, body),
  reject: (token: string, actionId: string, reason: string) => request<unknown>(token, "POST", `/v1/actions/${actionId}/reject`, { reason }),
  messages: (token: string, id: string) => request<Message[]>(token, "GET", `/v1/insurance-shopping-cases/${id}/messages`),
  sendMessage: (token: string, id: string, text: string) => request<{ reply: string }>(token, "POST", `/v1/insurance-shopping-cases/${id}/messages`, { text }),
  operatorCases: (token: string) => request<{ id: string; customer_id: string; status: string; version: number; review_reason: string | null; updated_at: string }[]>(token, "GET", "/v1/operator/cases"),
  operatorCase: (token: string, id: string) => request<OperatorCase>(token, "GET", `/v1/operator/cases/${id}`),
  operatorMetrics: (token: string) => request<Record<string, unknown>>(token, "GET", "/v1/operator/metrics"),
  workerRunOnce: (token: string) => request<{ jobs: unknown[] }>(token, "POST", "/v1/operator/worker/run-once"),
  advanceClock: (token: string, body: { minutes?: number; hours?: number; days?: number }) => request<{ now: string }>(token, "POST", "/v1/operator/clock/advance", body),
  resolveReview: (token: string, id: string, resolution: string) => request<unknown>(token, "POST", `/v1/operator/cases/${id}/resolve-review`, { resolution }),
};

export function money(minor: number | null | undefined): string {
  if (minor === null || minor === undefined) return "unknown";
  return `$${(minor / 100).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}
