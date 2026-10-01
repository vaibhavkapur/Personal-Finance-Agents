-- 001_initial.sql: PostgreSQL schema for the insurance shopping agent prototype.
-- Generated from backend/app/persistence/models.py (SQLAlchemy metadata). Local dev uses create_all().
BEGIN;

CREATE TABLE cases (
	id VARCHAR(64) NOT NULL, 
	tenant_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	workflow_type VARCHAR(64) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	version INTEGER NOT NULL, 
	current_needs_id VARCHAR(64), 
	selected_quote_id VARCHAR(64), 
	current_application_id VARCHAR(64), 
	review_reason TEXT, 
	completion_evidence_ref VARCHAR(128), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_cases_customer_id ON cases (customer_id);
CREATE INDEX ix_cases_tenant_id ON cases (tenant_id);
CREATE INDEX ix_cases_state ON cases (state);

CREATE TABLE documents (
	id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	object_key VARCHAR(256) NOT NULL, 
	content_hash VARCHAR(80) NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	captured_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	extraction_version VARCHAR(32) NOT NULL, 
	content_json JSON NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_documents_case_id ON documents (case_id);
CREATE INDEX ix_documents_owner_id ON documents (owner_id);

CREATE TABLE inbox_events (
	id VARCHAR(64) NOT NULL, 
	provider VARCHAR(64) NOT NULL, 
	external_event_id VARCHAR(128) NOT NULL, 
	payload_json JSON NOT NULL, 
	received_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	processed_at TIMESTAMP WITH TIME ZONE, 
	status VARCHAR(16) NOT NULL, 
	error TEXT, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_inbox_provider_event UNIQUE (provider, external_event_id)
);

CREATE TABLE jobs (
	id VARCHAR(64) NOT NULL, 
	type VARCHAR(64) NOT NULL, 
	payload_json JSON NOT NULL, 
	dedupe_key VARCHAR(160), 
	run_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	lease_until TIMESTAMP WITH TIME ZONE, 
	lease_owner VARCHAR(64), 
	attempts INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (dedupe_key)
);
CREATE INDEX ix_jobs_claim ON jobs (status, run_at, lease_until);
CREATE INDEX ix_jobs_run_at ON jobs (run_at);
CREATE INDEX ix_jobs_status ON jobs (status);
CREATE INDEX ix_jobs_type ON jobs (type);

CREATE TABLE outbox_events (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	event_type VARCHAR(96) NOT NULL, 
	payload_json JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	attempts INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	delivered_at TIMESTAMP WITH TIME ZONE, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_outbox_events_case_id ON outbox_events (case_id);

CREATE TABLE provider_requests (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	insurer_id VARCHAR(64) NOT NULL, 
	operation VARCHAR(64) NOT NULL, 
	request_ref VARCHAR(128), 
	request_redacted JSON NOT NULL, 
	response_redacted JSON, 
	environment VARCHAR(16) NOT NULL, 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_provider_requests_case_id ON provider_requests (case_id);

CREATE TABLE tool_runs (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	tool_name VARCHAR(64) NOT NULL, 
	input_redacted JSON NOT NULL, 
	output_ref VARCHAR(128), 
	started_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	finished_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	model_version VARCHAR(64) NOT NULL, 
	prompt_version VARCHAR(64) NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	error TEXT, 
	PRIMARY KEY (id)
);
CREATE INDEX ix_tool_runs_case_id ON tool_runs (case_id);

CREATE TABLE actions (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	type VARCHAR(64) NOT NULL, 
	payload_json JSON NOT NULL, 
	payload_hash VARCHAR(80) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	approval_id VARCHAR(64), 
	provider_ref VARCHAR(128), 
	idempotency_key VARCHAR(128) NOT NULL, 
	challenge_id VARCHAR(64) NOT NULL, 
	challenge_expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	expected_case_version INTEGER NOT NULL, 
	application_id VARCHAR(64), 
	result_json JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	UNIQUE (idempotency_key)
);
CREATE INDEX ix_actions_case_id ON actions (case_id);

CREATE TABLE case_events (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	sequence INTEGER NOT NULL, 
	event_type VARCHAR(96) NOT NULL, 
	source_event_id VARCHAR(128), 
	actor VARCHAR(64) NOT NULL, 
	occurred_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	from_state VARCHAR(32), 
	to_state VARCHAR(32), 
	expected_version INTEGER, 
	data_json JSON NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_case_event_sequence UNIQUE (case_id, sequence), 
	FOREIGN KEY(case_id) REFERENCES cases (id)
);
CREATE INDEX ix_case_events_case_id ON case_events (case_id);

CREATE TABLE conversation_messages (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	role VARCHAR(16) NOT NULL, 
	content TEXT NOT NULL, 
	tool_calls_json JSON, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id)
);
CREATE INDEX ix_conversation_messages_case_id ON conversation_messages (case_id);

CREATE TABLE insurance_needs (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	state_code VARCHAR(2) NOT NULL, 
	effective_date DATE, 
	version INTEGER NOT NULL, 
	property_limit_minor INTEGER, 
	liability_limit_minor INTEGER, 
	deductible_cap_minor INTEGER, 
	replacement_cost_required BOOLEAN, 
	data_json JSON NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_needs_case_version UNIQUE (case_id, version), 
	FOREIGN KEY(case_id) REFERENCES cases (id)
);
CREATE INDEX ix_insurance_needs_customer_id ON insurance_needs (customer_id);
CREATE INDEX ix_insurance_needs_case_id ON insurance_needs (case_id);

CREATE TABLE approvals (
	id VARCHAR(64) NOT NULL, 
	action_id VARCHAR(64) NOT NULL, 
	approver_id VARCHAR(64) NOT NULL, 
	action_hash VARCHAR(80) NOT NULL, 
	scope_json JSON NOT NULL, 
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	revoked_at TIMESTAMP WITH TIME ZONE, 
	consumed_at TIMESTAMP WITH TIME ZONE, 
	consumed_by VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(action_id) REFERENCES actions (id)
);
CREATE INDEX ix_approvals_action_id ON approvals (action_id);

CREATE TABLE quote_tasks (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	needs_id VARCHAR(64) NOT NULL, 
	needs_version INTEGER NOT NULL, 
	insurer_id VARCHAR(64) NOT NULL, 
	external_task_id VARCHAR(128), 
	correlation_id VARCHAR(64) NOT NULL, 
	request_ref VARCHAR(128) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	open_questions_json JSON NOT NULL, 
	environment VARCHAR(16) NOT NULL, 
	last_error TEXT, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_task_external UNIQUE (insurer_id, external_task_id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(needs_id) REFERENCES insurance_needs (id), 
	UNIQUE (request_ref)
);
CREATE INDEX ix_quote_tasks_insurer_id ON quote_tasks (insurer_id);
CREATE INDEX ix_quote_tasks_case_id ON quote_tasks (case_id);
CREATE INDEX ix_quote_tasks_correlation_id ON quote_tasks (correlation_id);

CREATE TABLE underwriting_answers (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	needs_id VARCHAR(64) NOT NULL, 
	question_id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64), 
	answer_json JSON NOT NULL, 
	confirmed_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	evidence_id VARCHAR(64), 
	answer_version INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_answer_version UNIQUE (case_id, question_id, answer_version), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(needs_id) REFERENCES insurance_needs (id)
);
CREATE INDEX ix_underwriting_answers_case_id ON underwriting_answers (case_id);

CREATE TABLE insurance_quotes (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	quote_task_id VARCHAR(64) NOT NULL, 
	insurer_id VARCHAR(64) NOT NULL, 
	needs_version INTEGER NOT NULL, 
	quote_version INTEGER NOT NULL, 
	quote_ref VARCHAR(128) NOT NULL, 
	annual_premium_minor INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	coverage_json JSON NOT NULL, 
	exclusions_json JSON NOT NULL, 
	policy_form_version VARCHAR(64) NOT NULL, 
	valid_until TIMESTAMP WITH TIME ZONE NOT NULL, 
	answers_hash VARCHAR(80) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	quote_json JSON NOT NULL, 
	superseded_by VARCHAR(64), 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_quote_ref_version UNIQUE (insurer_id, quote_ref, quote_version), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(quote_task_id) REFERENCES quote_tasks (id)
);
CREATE INDEX ix_insurance_quotes_insurer_id ON insurance_quotes (insurer_id);
CREATE INDEX ix_insurance_quotes_case_id ON insurance_quotes (case_id);

CREATE TABLE applications (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	quote_id VARCHAR(64) NOT NULL, 
	answers_hash VARCHAR(80) NOT NULL, 
	payload_json JSON NOT NULL, 
	payload_hash VARCHAR(80) NOT NULL, 
	submission_ref VARCHAR(128), 
	status VARCHAR(32) NOT NULL, 
	revision INTEGER NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	updated_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(quote_id) REFERENCES insurance_quotes (id)
);
CREATE INDEX ix_applications_case_id ON applications (case_id);

CREATE TABLE issued_policies (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	application_id VARCHAR(64) NOT NULL, 
	insurer_policy_ref VARCHAR(128), 
	effective_at DATE, 
	expires_at DATE, 
	declarations_document_id VARCHAR(64), 
	declarations_json JSON NOT NULL, 
	verified_against_quote_id VARCHAR(64) NOT NULL, 
	verification_json JSON NOT NULL, 
	verified BOOLEAN NOT NULL, 
	created_at TIMESTAMP WITH TIME ZONE NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(application_id) REFERENCES applications (id)
);
CREATE INDEX ix_issued_policies_case_id ON issued_policies (case_id);

COMMIT;