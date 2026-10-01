export type Json = Record<string, unknown>;

export interface Address {
  line1: string;
  city: string;
  state_code: string;
  postal_code: string;
}

export interface Needs {
  version: number;
  state_code: string;
  effective_date: string | null;
  property_limit_minor: number | null;
  liability_limit_minor: number | null;
  deductible_cap_minor: number | null;
  replacement_cost_required: boolean | null;
  required_item_classes: string[] | null;
  address: Address | null;
  deductible_preference: string | null;
}

export interface OutstandingQuestion {
  insurer_id: string;
  insurer_name?: string;
  question_id: string;
  text: string;
  type: string;
  choices?: string[] | null;
  reason: "blocks_quote" | "required_for_application";
  unknown_allowed?: boolean;
}

export interface QuoteTask {
  task_id: string;
  insurer_id: string;
  insurer_name: string;
  external_task_id: string | null;
  status: string;
  last_error: string | null;
  environment: string;
}

export interface QuoteSummary {
  quote_id: string;
  quote_ref: string;
  quote_version: number;
  insurer_id: string;
  insurer_name: string;
  annual_premium_minor: number;
  annual_premium_display: string;
  deductible_minor: number;
  policy_form_version: string;
  valid_until: string;
  status: string;
  source: { environment: string; authority: string; protocol?: string };
  revision_reason: string | null;
}

export interface Citation {
  clause_id: string;
  policy_form_version: string;
  title: string | null;
  text: string | null;
}

export interface Check {
  field: string;
  requirement: unknown;
  quote_value: unknown;
  result: "pass" | "fail" | "unknown";
  citation: Citation | null;
  explanation: string;
}

export interface ComparisonEntry {
  quote_id: string;
  quote_ref: string;
  insurer_id: string;
  insurer_name: string;
  annual_premium_display: string;
  deductible_minor: number;
  policy_form_version: string;
  valid_until: string;
  rank?: number;
  reason?: string;
  checks: Check[];
  failed_checks?: Check[];
  unknown_checks?: Check[];
}

export interface DifferenceRow {
  field: string;
  label: string;
  differs: boolean;
  note: string | null;
  values: Record<string, { insurer_id: string; value: unknown; display: string; citation: Citation | null }>;
}

export interface Comparison {
  suitable: ComparisonEntry[];
  excluded: ComparisonEntry[];
  undetermined: ComparisonEntry[];
  missing_responses: { insurer_id: string; insurer_name?: string; status: string; disclosure: string }[];
  differences: DifferenceRow[];
  trade_offs: string[];
  ranking: { preference: string; rule: string; disclosure: string };
  unknown_needs_fields: string[];
  complete: boolean;
}

export interface Review {
  action: string;
  destination: { insurer_id: string; insurer_name: string; environment: string; protocol: string };
  amount: { display: string };
  terms: {
    property_limit: string;
    liability_limit: string;
    deductible: string;
    replacement_cost: boolean;
    effective_date: string;
    policy_form_version: string;
    exclusions: string[];
    endorsements: string[];
  };
  answers: { question_id: string; question_text: string; value: unknown }[];
  documents_disclosed: string[];
  irreversible_effects: string[];
  expires_at: string;
  revision_reason?: string;
  previous_premium_display?: string;
  premium_change_display?: string;
}

export interface Action {
  action_id: string;
  type: string;
  status: string;
  payload_hash: string;
  challenge_id: string | null;
  challenge_expires_at: string;
  expected_case_version: number;
  review: Review | null;
  result: Json | null;
  created_at: string;
}

export interface Policy {
  policy_id: string;
  insurer_policy_ref: string | null;
  effective_at: string | null;
  expires_at: string | null;
  coverage_starts_in_future: boolean;
  coverage_label: string;
  verified: boolean;
  verification: { verified: boolean; mismatches: { field: string; detail: string }[] };
  declarations: Json;
}

export interface TimelineItem {
  label: string;
  event_type: string;
  at: string;
  state: string | null;
}

export interface CaseView {
  id: string;
  customer_id: string;
  status: string;
  version: number;
  environment: string;
  adapter_mode: string;
  review_reason: string | null;
  needs: Needs;
  missing_fields: string[];
  missing_for_comparison: string[];
  confirmed_answers: Record<string, { value: unknown; confirmed_at: string }>;
  outstanding_questions: OutstandingQuestion[];
  quote_tasks: QuoteTask[];
  quotes: QuoteSummary[];
  selected_quote_id: string | null;
  application: { application_id: string; status: string; revision: number; payload_hash: string } | null;
  pending_action: Action | null;
  actions: Action[];
  policy: Policy | null;
  timeline: TimelineItem[];
  next_decision: string;
}

export interface CaseListItem {
  id: string;
  status: string;
  version: number;
  updated_at: string;
}

export interface Message {
  id: string;
  role: "customer" | "agent";
  content: string;
  tool_calls: { name: string; ok: boolean }[] | null;
  at: string;
}

export interface OperatorCase extends CaseView {
  events: { sequence: number; type: string; actor: string; from: string | null; to: string | null; at: string; data: Json }[];
  provider_requests: { id: string; insurer_id: string; operation: string; request_ref: string | null; environment: string; latency_ms: number; outcome: string; at: string }[];
  tool_runs: { id: string; tool: string; latency_ms: number; model_version: string; outcome: string; at: string }[];
  jobs: { id: string; type: string; status: string; attempts: number; run_at: string; last_error: string | null }[];
}
