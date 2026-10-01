// Thin API client. The demo session is a header; a real deployment would use
// the identity provider's session cookie or token.

export const CUSTOMER_ID = "cus_demo_1";

export class ApiError extends Error {
  status: number;
  code: string;
  details: Record<string, unknown>;
  constructor(status: number, body: { error?: string; message?: string; details?: Record<string, unknown> } | null, fallback: string) {
    super(body?.message || fallback);
    this.status = status;
    this.code = body?.error || "error";
    this.details = body?.details || {};
  }
}

async function request<T>(path: string, init: RequestInit = {}, role: "customer" | "operator" = "customer"): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "X-Customer-Id": role === "operator" ? "ops_demo" : CUSTOMER_ID,
    ...(role === "operator" ? { "X-Role": "operator" } : {}),
  };
  const res = await fetch(path, { ...init, headers: { ...headers, ...(init.headers as Record<string, string> | undefined) } });
  const text = await res.text();
  const body = text ? JSON.parse(text) : null;
  if (!res.ok) throw new ApiError(res.status, body, `${res.status} ${res.statusText}`);
  return body as T;
}

export const api = {
  inbox: () => request<Inbox>("/v1/me/inbox"),
  cases: () => request<CaseSummary[]>("/v1/banking-cases"),
  createCase: (body: CreateCaseBody) => request<CreateCaseResponse>("/v1/banking-cases", { method: "POST", body: JSON.stringify(body) }),
  getCase: (id: string) => request<CaseDetail>(`/v1/banking-cases/${id}`),
  answer: (id: string, body: Record<string, unknown>) => request<CaseSummary>(`/v1/banking-cases/${id}/answers`, { method: "POST", body: JSON.stringify(body) }),
  evaluate: (id: string) => request<Evaluation>(`/v1/banking-cases/${id}/evaluate`, { method: "POST" }),
  prepare: (id: string, option_id: string, amount_minor?: number) =>
    request<Review>(`/v1/banking-cases/${id}/instructions`, { method: "POST", body: JSON.stringify({ option_id, amount_minor }) }),
  timeline: (id: string) => request<Timeline>(`/v1/banking-cases/${id}/timeline`),
  cancel: (id: string) => request<CaseSummary>(`/v1/banking-cases/${id}/cancel`, { method: "POST" }),
  challenge: (actionId: string) => request<Challenge>(`/v1/actions/${actionId}/challenge`, { method: "POST" }),
  approve: (actionId: string, body: { expected_case_version: number; action_payload_hash: string; approval_challenge_id: string }) =>
    request<ApproveResponse>(`/v1/actions/${actionId}/approve`, { method: "POST", body: JSON.stringify(body) }),
  agentTurn: (message: string, case_id?: string | null) => request<AgentTurn>("/v1/agent/turns", { method: "POST", body: JSON.stringify({ message, case_id }) }),
  // operator
  overview: () => request<Overview>("/v1/operator/overview", {}, "operator"),
  ledger: () => request<Ledger>("/v1/operator/mock-bank/ledger", {}, "operator"),
  inboxEvents: () => request<InboxEvent[]>("/v1/operator/inbox", {}, "operator"),
  capabilities: () => request<Capability[]>("/v1/operator/capabilities", {}, "operator"),
  advance: (days: number) => request<{ now: string }>("/v1/operator/clock/advance", { method: "POST", body: JSON.stringify({ days }) }, "operator"),
  runWorker: () => request<{ ran: number; results: { type: string; outcome: string }[] }>("/v1/operator/worker/run", { method: "POST" }, "operator"),
  submitMode: (mode: string) => request<unknown>("/v1/operator/mock-bank/submit-mode", { method: "POST", body: JSON.stringify({ mode }) }, "operator"),
  offerMode: (mode: string) => request<unknown>("/v1/operator/mock-bank/offer-mode", { method: "POST", body: JSON.stringify({ mode }) }, "operator"),
  revoke: (account_id: string, revoked: boolean) => request<unknown>("/v1/operator/mock-bank/revoke-access", { method: "POST", body: JSON.stringify({ account_id, revoked }) }, "operator"),
  replay: (inboxId: string) => request<unknown>(`/v1/operator/inbox/${inboxId}/replay`, { method: "POST" }, "operator"),
  reset: () => request<unknown>("/v1/operator/reset", { method: "POST" }, "operator"),
};

// ---- types (subset of the API contract, see docs/api.md) ----

export interface Inbox {
  customer_id: string;
  today: string;
  deposits: {
    deposit_id: string;
    account: { id: string; display_name: string; provider_id: string };
    principal_minor: number;
    currency: string;
    apy_decimal: string;
    maturity_date: string;
    days_to_maturity: number;
    renewal_instruction_deadline: string;
    default_maturity_behavior: string;
    contract_version: string;
    case: CaseSummary | null;
  }[];
  obligations: Obligation[];
}

export interface Obligation {
  id: string;
  description: string;
  amount_minor: number;
  currency: string;
  due_date: string;
  certainty: string;
  evidence_id: string | null;
  selected?: boolean;
}

export interface CreateCaseBody {
  customer_id: string;
  deposit_id: string;
  currency: string;
  minimum_buffer_minor: number;
  obligation_ids: string[];
  preferred_lockup_days?: number | null;
  buffer_includes_obligations?: boolean | null;
}

export interface CreateCaseResponse {
  id: string;
  status: string;
  version: number;
  missing_fields: string[];
  outstanding_questions: Question[];
}

export interface Question {
  field: string;
  question: string;
  why: string;
}

export interface CaseSummary {
  id: string;
  state: string;
  version: number;
  deposit_id: string;
  maturity_date: string | null;
  currency: string;
  minimum_buffer_minor: number;
  obligation_ids: string[];
  preferred_lockup_days: number | null;
  buffer_includes_obligations: boolean | null;
  missing_fields: string[];
  outstanding_questions: Question[];
  warnings: string[];
  selected_offer_id: string | null;
  plan_id: string | null;
  current_action_id: string | null;
  review_reason: string | null;
  completion_evidence_ref: string | null;
  created_at: string;
  updated_at: string;
}

export interface Account {
  id: string;
  display_name: string;
  provider_id: string;
  account_kind: string;
  ownership_verified: boolean;
  access_revoked: boolean;
  currency: string;
  available_minor: number;
  current_minor: number;
  pending: { description: string; amount_minor: number; expected_on: string }[];
  snapshot_at: string;
  snapshot_source: string;
  evidence_id: string | null;
}

export interface Option {
  offer_id: string;
  product_version: string;
  comparable: boolean;
  exclusion_reasons: string[];
  warnings: string[];
  earnings_to_term_minor: number | null;
  earnings_at_horizon_minor: number | null;
  fees_minor: number;
  net_at_horizon_minor: number | null;
  locked_until: string | null;
  liquid: boolean;
  early_withdrawal_note: string | null;
  assumptions: string[];
  source: { provider_id: string; product_version: string; retrieved_at: string; environment: string; authoritative: boolean; evidence_id: string | null };
  offer: {
    id: string;
    provider_id: string;
    product_name: string;
    product_version: string;
    offer_kind: string;
    apy_decimal: string;
    rate_type: string;
    term_days: number | null;
    fees_minor: number;
    restrictions: Record<string, unknown>;
    valid_until: string;
    eligibility_status: string;
    eligibility_notes: string | null;
    destination_account_id: string | null;
  };
}

export interface ProjectionDay {
  date: string;
  opening_minor: number;
  inflows_minor: number;
  obligations_minor: number;
  allocations_minor: number;
  closing_minor: number;
  events: { kind: string; label: string; amount_minor: number; ref: string | null; confirmed: boolean; source: string | null }[];
}

export interface Projection {
  buffer_minor: number;
  effective_date: string;
  lowest_balance_minor: number;
  lowest_from_effective_minor: number;
  lowest_from_effective_date: string;
  feasible: boolean;
  allocations_minor: number;
  breaches: { date: string; balance_minor: number; shortfall_minor: number; before_effective: boolean }[];
  pre_effective_breaches: { date: string; balance_minor: number }[];
  days?: ProjectionDay[];
}

export interface Assumption {
  kind: string;
  text: string;
  source: Record<string, unknown>;
}

export interface Plan {
  id: string;
  version: number;
  status: string;
  max_lockable_minor: number;
  allocation_minor: number;
  lowest_balance_minor: number;
  offer_id: string | null;
  offer_product_version: string | null;
  projection: Projection;
  options: Option[];
  assumptions: Assumption[];
  inputs_hash: string;
}

export interface Review {
  case_id: string;
  case_version: number;
  state: string;
  action_id: string;
  action_status: string;
  action_payload_hash: string;
  idempotency_key: string;
  request_ref: string;
  instruction: Record<string, unknown> & { instruction_type: string; amount_minor: number; currency: string; effective_at: string; destination_account_id: string | null; term_days: number | null; apy_decimal: string; product_version: string };
  amount_display: string;
  source_account: { id: string; display_name: string; provider_id: string };
  destination_account: { id: string; display_name: string; provider_id: string; ownership_verified: boolean } | null;
  offer: { id: string; product_name: string; product_version: string; apy_decimal: string; term_days: number | null; fees_minor: number; restrictions: Record<string, unknown>; valid_until: string; source: { provider_id: string; retrieved_at: string; environment: string; authoritative: boolean } } | null;
  irreversible_effect: string;
  liquidity: { lowest_balance_after_allocation_minor: number; buffer_minor: number; reserved_minor: number; feasible: boolean } | null;
  assumptions_requiring_confirmation: Assumption[];
  documents: { id: string; summary: string | null; content_hash: string; source: string; captured_at: string }[];
  approvals: { id: string; approver_id: string; expires_at: string; revoked_at: string | null; consumed_at: string | null }[];
}

export interface Instruction {
  id: string;
  status: string;
  instruction_type: string;
  amount_minor: number;
  currency: string;
  effective_at: string;
  external_ref: string | null;
  request_ref: string;
  source_account_id: string;
  destination_account_id: string | null;
  offer_id: string;
  product_version: string;
  reconciliation: { matched: boolean; checks: { check: string; expected: unknown; actual: unknown; ok: boolean }[]; evidence_refs: string[] } | null;
}

export interface CaseDetail extends CaseSummary {
  deposit: { id: string; account_id: string; display_name: string; principal_minor: number; currency: string; apy_decimal: string; maturity_date: string; renewal_instruction_deadline: string; default_maturity_behavior: string; contract_version: string; evidence_id: string | null };
  accounts: Account[];
  obligations: Obligation[];
  plan: Plan | null;
  review: Review | null;
  instruction: Instruction | null;
  now: string;
}

export interface Evaluation {
  plan_id: string;
  max_lockable_minor: number;
  reserved_minor: number;
  effective_buffer_minor: number;
  options: Option[];
  warnings: string[];
  case: CaseSummary;
}

export interface Challenge {
  approval_challenge_id: string;
  action_id: string;
  action_payload_hash: string;
  expected_case_version: number;
  expires_at: string;
}

export interface ApproveResponse {
  approval_id: string;
  case_status: string;
  case_version: number;
  expires_at: string;
}

export interface Timeline {
  case_id: string;
  state: string;
  version: number;
  events: { id: string; sequence: number; type: string; actor: string; previous_state: string | null; next_state: string | null; expected_case_version: number | null; source_event_id: string | null; occurred_at: string; data: Record<string, unknown> }[];
  provider_requests: { id: string; provider_id: string; operation: string; request_ref: string | null; environment: string; latency_ms: number; outcome: string; created_at: string; response: Record<string, unknown> }[];
  instruction: Instruction | null;
  completion_evidence_ref: string | null;
}

export interface AgentTurn {
  reply: string;
  case_id: string | null;
  state: string | null;
  tool_calls: { tool: string; arguments: Record<string, unknown>; outcome: string }[];
  questions: string[];
  refused: boolean;
  escalated: boolean;
  budget_exhausted: boolean;
  model_version: string;
}

export interface Overview {
  now: string;
  environment: string;
  cases_by_state: Record<string, number>;
  cases_waiting_on_customer: number;
  cases_waiting_on_provider: number;
  cases_in_manual_review: number;
  queue: { pending: number; leased: number; failed: number; oldest_pending_run_at: string | null };
  duplicate_actions_prevented: number;
  tool_errors: number;
  mock_bank: { next_submit_mode: string; offer_mode: string };
  pending_actions: { id: string; case_id: string; type: string; status: string; payload_hash: string; provider_reference: string | null; request_ref: string; idempotency_key: string; last_error: string | null; updated_at: string }[];
  jobs: { id: string; type: string; case_id: string | null; status: string; run_at: string; attempts: number; lease_owner: string | null; last_error: string | null }[];
  adapter_requests: { id: string; provider_id: string; operation: string; request_ref: string | null; case_id: string | null; environment: string; latency_ms: number; outcome: string; created_at: string; response: Record<string, unknown> }[];
  tool_runs: { id: string; case_id: string | null; tool_name: string; outcome: string; latency_ms: number; model_version: string; prompt_version: string; created_at: string }[];
  cases: { id: string; customer_id: string; state: string; version: number; deposit_id: string; review_reason: string | null; updated_at: string }[];
}

export interface Ledger {
  accounts: { id: string; provider_id: string; kind: string; currency: string; available_minor: number; current_minor: number; access_revoked: boolean }[];
  deposits: { id: string; account_id: string; principal_minor: number; apy_decimal: string; term_days: number; opened_on: string; maturity_date: string; product_version: string; status: string; matured_from_id: string | null }[];
  instructions: { request_ref: string; provider_reference: string; status: string; decline_reason: string | null; effective_on: string | null; applied: boolean; callback_sent: boolean; credited_amount_minor: number | null; amount_minor: number; instruction_type: string }[];
}

export interface InboxEvent {
  id: string;
  provider_id: string;
  event_id: string;
  event_type: string;
  signature_valid: boolean;
  received_at: string;
  processed_at: string | null;
  result: string | null;
  replay_count: number;
}

export interface Capability {
  provider_id: string;
  environment: string;
  can_read_snapshots: boolean;
  can_read_offers: boolean;
  can_submit_instructions: boolean;
  can_lookup_by_request_ref: boolean;
  can_cancel_after_acceptance: boolean;
  supports_callbacks: boolean;
  notes: string;
}

export function money(minor: number | null | undefined, currency = "USD"): string {
  if (minor === null || minor === undefined) return "—";
  const sign = minor < 0 ? "-" : "";
  const abs = Math.abs(minor);
  const whole = Math.floor(abs / 100).toLocaleString("en-US");
  const cents = String(abs % 100).padStart(2, "0");
  return currency === "USD" ? `${sign}$${whole}.${cents}` : `${sign}${whole}.${cents} ${currency}`;
}

export function pct(rate: string): string {
  return `${(parseFloat(rate) * 100).toFixed(2)}%`;
}
