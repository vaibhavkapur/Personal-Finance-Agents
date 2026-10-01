export interface Amounts {
  currency: string;
  requested_minor: number;
  promised_minor: number;
  target_minor: number;
  final_recovered_minor: number;
  provisional_minor: number;
  store_credit_minor: number;
  reversed_minor: number;
  outstanding_minor: number;
  final_by_channel: Record<string, number>;
  overlap_flagged: boolean;
  overlap_minor: number;
}

export interface PendingQuestion {
  kind: string;
  prompt: string;
  options: Array<Record<string, any>>;
}

export interface Channel {
  id: string;
  channel_type: "merchant" | "issuer";
  provider: string;
  provider_case_ref: string | null;
  status: string;
  deadline_at: string | null;
  deadline_source: string | null;
  followup_count: number;
  last_provider_status: string | null;
}

export interface CaseStatus {
  case_id: string;
  status: string;
  version: number;
  reason_code: string;
  order_ref: string;
  merchant_id: string;
  amounts: Amounts;
  promise: { promised_minor: number; promised_at: string; promised_by: string; destination_type: string; provider_refund_ref: string | null } | null;
  channels: Channel[];
  pending_question: PendingQuestion | null;
  deadline: { deadline_at: string; source: string; alert: string | null; fixture: boolean } | null;
  completion_evidence_ref: string | null;
  outcome_note: string | null;
  next_step: string;
  environment: string;
}

export interface ReviewScreen {
  lane: "merchant" | "issuer";
  destination: Record<string, any>;
  documents: Array<{ document_id: string; kind: string; content_hash: string }>;
  amount_minor: number;
  currency: string;
  terms: Record<string, any>;
  irreversible_effect: string;
  subject?: string;
  body?: string;
  deadline?: Record<string, any> | null;
  environment: string;
}

export interface Draft {
  action_id: string;
  case_id: string;
  type: string;
  status: string;
  payload_hash: string;
  expected_case_version: number;
  approval_challenge_id: string | null;
  challenge_expires_at: string | null;
  review: ReviewScreen;
  next_step: string;
}

export interface AgentTurn {
  case_id: string;
  status: string;
  summary: string;
  questions: PendingQuestion[];
  proposed_action: (Draft & { review: ReviewScreen }) | null;
  tool_calls: Array<{ name: string; ok: boolean }>;
  escalation: Record<string, any> | null;
  guardrail: { blocked: boolean };
}

export interface TimelineEvent {
  sequence: number;
  event_type: string;
  actor: string;
  occurred_at: string;
  previous_state: string | null;
  next_state: string | null;
  data: Record<string, any>;
}

export interface Timeline {
  events: TimelineEvent[];
  channels: Channel[];
  credit_matches: Array<Record<string, any>>;
  actions: Array<Record<string, any>>;
  amounts: Amounts;
}
