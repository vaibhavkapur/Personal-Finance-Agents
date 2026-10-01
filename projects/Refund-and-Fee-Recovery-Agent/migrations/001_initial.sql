-- Initial schema. Written for SQLite (prototype) using only portable SQL so it
-- can be moved to PostgreSQL with minimal edits (TEXT timestamps -> TIMESTAMPTZ,
-- *_json TEXT -> JSONB).
--
-- Money is stored as integer minor units with an explicit currency column.

CREATE TABLE IF NOT EXISTS customers (
  id TEXT PRIMARY KEY,
  tenant_id TEXT NOT NULL,
  display_name TEXT NOT NULL,
  email TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_instruments (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  network TEXT NOT NULL,
  last4 TEXT NOT NULL,
  issuer_id TEXT NOT NULL
);

-- Verified contact registry. Outbound recipients MUST come from here, never
-- from addresses found inside untrusted documents.
CREATE TABLE IF NOT EXISTS merchants (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  support_email TEXT NOT NULL,
  support_channel TEXT NOT NULL,
  contact_verified_at TEXT NOT NULL,
  contact_source TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  kind TEXT NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  source TEXT NOT NULL,
  captured_at TEXT NOT NULL,
  extraction_version TEXT NOT NULL,
  extracted_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS purchase_records (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  merchant_id TEXT NOT NULL REFERENCES merchants(id),
  order_ref TEXT NOT NULL,
  original_transaction_id TEXT NOT NULL,
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  payment_instrument_ref TEXT NOT NULL,
  purchased_at TEXT NOT NULL,
  UNIQUE (customer_id, order_ref)
);

CREATE TABLE IF NOT EXISTS refund_promises (
  id TEXT PRIMARY KEY,
  purchase_id TEXT NOT NULL REFERENCES purchase_records(id),
  promised_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  destination_type TEXT NOT NULL,          -- original_payment | store_credit | unknown
  promised_by TEXT NOT NULL,
  promised_at TEXT NOT NULL,
  expected_by TEXT,
  evidence_id TEXT REFERENCES documents(id),
  provider_refund_ref TEXT,
  verified_at TEXT
);

-- Account feed. direction is explicit; amounts are always positive.
CREATE TABLE IF NOT EXISTS transactions (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  payment_instrument_ref TEXT NOT NULL,
  merchant_id TEXT,
  direction TEXT NOT NULL,                 -- debit | credit
  kind TEXT NOT NULL,                      -- purchase | refund | provisional_credit | reversal | fee | other
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  posted_at TEXT NOT NULL,
  description TEXT NOT NULL,
  provider_ref TEXT,
  reverses_transaction_id TEXT,
  source TEXT NOT NULL,
  UNIQUE (source, provider_ref)
);

CREATE TABLE IF NOT EXISTS recovery_cases (
  id TEXT PRIMARY KEY,
  customer_id TEXT NOT NULL REFERENCES customers(id),
  purchase_id TEXT NOT NULL REFERENCES purchase_records(id),
  promise_id TEXT REFERENCES refund_promises(id),
  reason_code TEXT NOT NULL,
  target_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  final_recovered_minor INTEGER NOT NULL DEFAULT 0,
  provisional_minor INTEGER NOT NULL DEFAULT 0,
  store_credit_minor INTEGER NOT NULL DEFAULT 0,
  reversed_minor INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1,
  pending_question_json TEXT,
  completion_evidence_ref TEXT,
  outcome_note TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recovery_channels (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES recovery_cases(id),
  channel_type TEXT NOT NULL,              -- merchant | issuer
  provider TEXT NOT NULL,
  provider_case_ref TEXT,
  status TEXT NOT NULL,
  deadline_at TEXT,
  deadline_source TEXT,
  followup_count INTEGER NOT NULL DEFAULT 0,
  last_contact_at TEXT,
  next_followup_at TEXT,
  last_provider_status TEXT,
  created_at TEXT NOT NULL,
  UNIQUE (provider, provider_case_ref)
);

CREATE TABLE IF NOT EXISTS credit_matches (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES recovery_cases(id),
  transaction_id TEXT NOT NULL REFERENCES transactions(id),
  amount_minor INTEGER NOT NULL,
  currency TEXT NOT NULL,
  credit_kind TEXT NOT NULL,               -- final | provisional | store_credit
  channel_type TEXT NOT NULL,              -- merchant | issuer | unknown
  matching_method TEXT NOT NULL,           -- provider_reference | amount_merchant_window | customer_confirmed
  confidence REAL NOT NULL,
  confirmed_by TEXT NOT NULL,
  confirmed_at TEXT NOT NULL,
  reversed_at TEXT,
  reversal_transaction_id TEXT,
  UNIQUE (case_id, transaction_id)
);

CREATE TABLE IF NOT EXISTS outbound_packets (
  id TEXT PRIMARY KEY,
  channel_id TEXT NOT NULL REFERENCES recovery_channels(id),
  action_id TEXT NOT NULL,
  recipient_ref TEXT NOT NULL,
  recipient_address TEXT NOT NULL,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  message_hash TEXT NOT NULL,
  attachment_manifest_json TEXT NOT NULL,
  approval_id TEXT,
  provider_message_ref TEXT,
  sent_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS actions (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES recovery_cases(id),
  channel_id TEXT REFERENCES recovery_channels(id),
  type TEXT NOT NULL,                      -- send_merchant_message | send_merchant_followup | submit_issuer_dispute
  payload_json TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  status TEXT NOT NULL,                    -- awaiting_approval | approved | submitting | submitted | unknown | failed | declined | expired | superseded
  approval_id TEXT,
  provider_ref TEXT,
  idempotency_key TEXT UNIQUE,
  request_ref TEXT NOT NULL UNIQUE,
  expected_case_version INTEGER NOT NULL,
  failure_reason TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS approvals (
  id TEXT PRIMARY KEY,
  action_id TEXT NOT NULL REFERENCES actions(id),
  approver_id TEXT,
  action_hash TEXT NOT NULL,
  scope_json TEXT NOT NULL,
  challenge_id TEXT NOT NULL UNIQUE,
  challenge_expires_at TEXT NOT NULL,
  expected_case_version INTEGER NOT NULL,
  approved_at TEXT,
  revoked_at TEXT,
  revoke_reason TEXT,
  consumed_at TEXT,
  consumed_by_action_id TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS case_events (
  id TEXT PRIMARY KEY,
  case_id TEXT NOT NULL REFERENCES recovery_cases(id),
  sequence INTEGER NOT NULL,
  event_type TEXT NOT NULL,
  source_event_id TEXT,
  actor TEXT NOT NULL,
  occurred_at TEXT NOT NULL,
  previous_state TEXT,
  next_state TEXT,
  expected_version INTEGER,
  data_json TEXT NOT NULL,
  UNIQUE (case_id, sequence)
);

-- Event inbox: at-least-once delivery from providers, deduplicated here.
CREATE TABLE IF NOT EXISTS provider_events_inbox (
  provider TEXT NOT NULL,
  event_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  environment TEXT NOT NULL,
  received_at TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  processed_at TEXT,
  outcome TEXT,
  PRIMARY KEY (provider, event_id)
);

-- Transactional outbox for our own application events / notifications.
CREATE TABLE IF NOT EXISTS outbox (
  id TEXT PRIMARY KEY,
  event_type TEXT NOT NULL,
  case_id TEXT,
  occurred_at TEXT NOT NULL,
  environment TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  signature TEXT NOT NULL,
  dispatched_at TEXT
);

CREATE TABLE IF NOT EXISTS tool_runs (
  id TEXT PRIMARY KEY,
  case_id TEXT,
  tool_name TEXT NOT NULL,
  input_ref TEXT NOT NULL,
  output_ref TEXT NOT NULL,
  source_ts TEXT NOT NULL,
  latency_ms INTEGER NOT NULL,
  model_version TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  outcome TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Persisted jobs with leases for the single worker.
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  type TEXT NOT NULL,
  case_id TEXT,
  payload_json TEXT NOT NULL,
  run_at TEXT NOT NULL,
  lease_owner TEXT,
  lease_until TEXT,
  attempts INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,                    -- pending | running | done | failed
  dedupe_key TEXT UNIQUE,
  last_error TEXT,
  created_at TEXT NOT NULL,
  completed_at TEXT
);

-- Operator view of adapter calls (redacted).
CREATE TABLE IF NOT EXISTS provider_requests (
  id TEXT PRIMARY KEY,
  provider TEXT NOT NULL,
  environment TEXT NOT NULL,
  operation TEXT NOT NULL,
  request_ref TEXT,
  case_id TEXT,
  request_summary_json TEXT NOT NULL,
  response_summary_json TEXT,
  status TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_transactions_customer ON transactions(customer_id, payment_instrument_ref);
CREATE INDEX IF NOT EXISTS idx_cases_customer ON recovery_cases(customer_id, status);
CREATE INDEX IF NOT EXISTS idx_jobs_due ON jobs(status, run_at);
CREATE INDEX IF NOT EXISTS idx_case_events_case ON case_events(case_id, sequence);
