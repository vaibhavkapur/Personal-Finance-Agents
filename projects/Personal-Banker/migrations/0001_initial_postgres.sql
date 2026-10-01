-- Generated from app/persistence/models.py for postgresql. Do not edit by hand; re-run scripts/dump_schema.py.

CREATE TABLE customers (
	id VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE providers (
	id VARCHAR(64) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	environment VARCHAR(16) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE documents (
	id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(64) NOT NULL, 
	object_key VARCHAR(256) NOT NULL, 
	content_hash VARCHAR(80) NOT NULL, 
	source VARCHAR(64) NOT NULL, 
	captured_at VARCHAR(32) NOT NULL, 
	extraction_version VARCHAR(32) NOT NULL, 
	summary VARCHAR(256), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_documents_owner_id ON documents (owner_id);

CREATE TABLE tool_runs (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	customer_id VARCHAR(64), 
	tool_name VARCHAR(64) NOT NULL, 
	input_ref VARCHAR(80) NOT NULL, 
	output_ref VARCHAR(80) NOT NULL, 
	input_redacted_json JSON NOT NULL, 
	output_summary_json JSON NOT NULL, 
	source_timestamps_json JSON NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	model_version VARCHAR(64) NOT NULL, 
	prompt_version VARCHAR(32) NOT NULL, 
	outcome VARCHAR(16) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_tool_runs_case_id ON tool_runs (case_id);

CREATE TABLE outbox_messages (
	id VARCHAR(64) NOT NULL, 
	topic VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64), 
	payload_json JSON NOT NULL, 
	signature VARCHAR(80) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	published_at VARCHAR(32), 
	PRIMARY KEY (id)
);

CREATE TABLE inbox_events (
	id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	event_id VARCHAR(64) NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	payload_json JSON NOT NULL, 
	signature_valid BOOLEAN NOT NULL, 
	received_at VARCHAR(32) NOT NULL, 
	processed_at VARCHAR(32), 
	result VARCHAR(256), 
	replay_count INTEGER NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (provider_id, event_id)
);

CREATE TABLE jobs (
	id VARCHAR(64) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	case_id VARCHAR(64), 
	payload_json JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	run_at VARCHAR(32) NOT NULL, 
	lease_until VARCHAR(32), 
	lease_owner VARCHAR(64), 
	attempts INTEGER NOT NULL, 
	last_error TEXT, 
	created_at VARCHAR(32) NOT NULL, 
	finished_at VARCHAR(32), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_jobs_case_id ON jobs (case_id);

CREATE TABLE adapter_requests (
	id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	operation VARCHAR(64) NOT NULL, 
	request_ref VARCHAR(80), 
	case_id VARCHAR(64), 
	request_redacted_json JSON NOT NULL, 
	response_redacted_json JSON NOT NULL, 
	environment VARCHAR(16) NOT NULL, 
	latency_ms INTEGER NOT NULL, 
	outcome VARCHAR(32) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_adapter_requests_case_id ON adapter_requests (case_id);

CREATE TABLE sim_clock (
	id INTEGER NOT NULL, 
	now VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE mock_bank_accounts (
	id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	owner_id VARCHAR(64) NOT NULL, 
	kind VARCHAR(32) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	available_minor INTEGER NOT NULL, 
	current_minor INTEGER NOT NULL, 
	pending_json JSON NOT NULL, 
	access_revoked BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mock_bank_accounts_provider_id ON mock_bank_accounts (provider_id);

CREATE TABLE mock_bank_deposits (
	id VARCHAR(64) NOT NULL, 
	account_id VARCHAR(64) NOT NULL, 
	principal_minor INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	apy_decimal VARCHAR(16) NOT NULL, 
	term_days INTEGER NOT NULL, 
	opened_on DATE NOT NULL, 
	maturity_date DATE NOT NULL, 
	product_version VARCHAR(32) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	matured_from_id VARCHAR(64), 
	maturity_proceeds_account_id VARCHAR(64), 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mock_bank_deposits_account_id ON mock_bank_deposits (account_id);

CREATE TABLE mock_bank_offers (
	id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	for_deposit_id VARCHAR(64) NOT NULL, 
	payload_json JSON NOT NULL, 
	active BOOLEAN NOT NULL, 
	PRIMARY KEY (id)
);

CREATE INDEX ix_mock_bank_offers_for_deposit_id ON mock_bank_offers (for_deposit_id);

CREATE TABLE mock_bank_instructions (
	request_ref VARCHAR(80) NOT NULL, 
	provider_reference VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	payload_json JSON NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	decline_reason VARCHAR(128), 
	effective_on DATE, 
	applied BOOLEAN NOT NULL, 
	callback_delay_days INTEGER NOT NULL, 
	callback_sent BOOLEAN NOT NULL, 
	credited_amount_minor INTEGER, 
	accepted_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (request_ref), 
	UNIQUE (provider_reference)
);

CREATE TABLE mock_bank_config (
	id INTEGER NOT NULL, 
	next_submit_mode VARCHAR(32) NOT NULL, 
	offer_mode VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id)
);

CREATE TABLE bank_accounts (
	id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	account_kind VARCHAR(32) NOT NULL, 
	display_name VARCHAR(128) NOT NULL, 
	ownership_verified BOOLEAN NOT NULL, 
	access_revoked BOOLEAN NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	available_minor INTEGER NOT NULL, 
	current_minor INTEGER NOT NULL, 
	pending_json JSON NOT NULL, 
	snapshot_at VARCHAR(32) NOT NULL, 
	snapshot_source VARCHAR(32) NOT NULL, 
	evidence_id VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(provider_id) REFERENCES providers (id), 
	FOREIGN KEY(evidence_id) REFERENCES documents (id)
);

CREATE INDEX ix_bank_accounts_customer_id ON bank_accounts (customer_id);

CREATE TABLE obligations (
	id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	description VARCHAR(128) NOT NULL, 
	amount_minor INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	due_date DATE NOT NULL, 
	certainty VARCHAR(16) NOT NULL, 
	evidence_id VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(evidence_id) REFERENCES documents (id)
);

CREATE INDEX ix_obligations_customer_id ON obligations (customer_id);

CREATE TABLE deposit_offers (
	id VARCHAR(64) NOT NULL, 
	provider_id VARCHAR(64) NOT NULL, 
	product_code VARCHAR(64) NOT NULL, 
	product_name VARCHAR(128) NOT NULL, 
	product_version VARCHAR(32) NOT NULL, 
	offer_kind VARCHAR(32) NOT NULL, 
	apy_decimal VARCHAR(16) NOT NULL, 
	rate_type VARCHAR(16) NOT NULL, 
	term_days INTEGER, 
	fees_minor INTEGER NOT NULL, 
	fee_description VARCHAR(256), 
	restrictions_json JSON NOT NULL, 
	accrual_method VARCHAR(32) NOT NULL, 
	rounding VARCHAR(32) NOT NULL, 
	valid_until DATE NOT NULL, 
	eligibility_status VARCHAR(16) NOT NULL, 
	eligibility_notes VARCHAR(256), 
	destination_account_id VARCHAR(64), 
	for_deposit_id VARCHAR(64) NOT NULL, 
	retrieved_at VARCHAR(32) NOT NULL, 
	environment VARCHAR(16) NOT NULL, 
	authoritative BOOLEAN NOT NULL, 
	evidence_id VARCHAR(64), 
	superseded BOOLEAN NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (provider_id, product_code, product_version), 
	FOREIGN KEY(provider_id) REFERENCES providers (id), 
	FOREIGN KEY(evidence_id) REFERENCES documents (id)
);

CREATE INDEX ix_deposit_offers_for_deposit_id ON deposit_offers (for_deposit_id);

CREATE TABLE deposit_contracts (
	id VARCHAR(64) NOT NULL, 
	account_id VARCHAR(64) NOT NULL, 
	principal_minor INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	apy_decimal VARCHAR(16) NOT NULL, 
	maturity_date DATE NOT NULL, 
	renewal_instruction_deadline DATE NOT NULL, 
	grace_period_days INTEGER NOT NULL, 
	contract_version VARCHAR(32) NOT NULL, 
	default_maturity_behavior VARCHAR(64) NOT NULL, 
	evidence_id VARCHAR(64), 
	PRIMARY KEY (id), 
	FOREIGN KEY(account_id) REFERENCES bank_accounts (id), 
	FOREIGN KEY(evidence_id) REFERENCES documents (id)
);

CREATE INDEX ix_deposit_contracts_account_id ON deposit_contracts (account_id);

CREATE TABLE cases (
	id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	workflow_type VARCHAR(32) NOT NULL, 
	state VARCHAR(32) NOT NULL, 
	version INTEGER NOT NULL, 
	deposit_id VARCHAR(64) NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	minimum_buffer_minor INTEGER NOT NULL, 
	obligation_ids_json JSON NOT NULL, 
	preferred_lockup_days INTEGER, 
	buffer_includes_obligations BOOLEAN, 
	concentration_limit_minor INTEGER, 
	missing_fields_json JSON NOT NULL, 
	outstanding_questions_json JSON NOT NULL, 
	warnings_json JSON NOT NULL, 
	provider_mode VARCHAR(32) NOT NULL, 
	selected_offer_id VARCHAR(64), 
	plan_id VARCHAR(64), 
	current_action_id VARCHAR(64), 
	review_reason TEXT, 
	completion_evidence_ref VARCHAR(128), 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(deposit_id) REFERENCES deposit_contracts (id)
);

CREATE INDEX ix_cases_customer_id ON cases (customer_id);

CREATE TABLE cash_plans (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	inputs_hash VARCHAR(80) NOT NULL, 
	projection_json JSON NOT NULL, 
	lowest_balance_minor INTEGER NOT NULL, 
	max_lockable_minor INTEGER NOT NULL, 
	allocation_minor INTEGER NOT NULL, 
	offer_id VARCHAR(64), 
	offer_product_version VARCHAR(32), 
	comparison_json JSON NOT NULL, 
	assumptions_json JSON NOT NULL, 
	version INTEGER NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(customer_id) REFERENCES customers (id), 
	FOREIGN KEY(offer_id) REFERENCES deposit_offers (id)
);

CREATE INDEX ix_cash_plans_case_id ON cash_plans (case_id);

CREATE TABLE actions (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	type VARCHAR(32) NOT NULL, 
	payload_json JSON NOT NULL, 
	payload_hash VARCHAR(80) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	approval_id VARCHAR(64), 
	provider_reference VARCHAR(64), 
	idempotency_key VARCHAR(80) NOT NULL, 
	request_ref VARCHAR(80) NOT NULL, 
	case_version_at_creation INTEGER NOT NULL, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	last_error TEXT, 
	PRIMARY KEY (id), 
	UNIQUE (idempotency_key), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	UNIQUE (request_ref)
);

CREATE INDEX ix_actions_case_id ON actions (case_id);

CREATE TABLE case_events (
	id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	sequence INTEGER NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	source_event_id VARCHAR(64), 
	actor VARCHAR(64) NOT NULL, 
	previous_state VARCHAR(32), 
	next_state VARCHAR(32), 
	expected_case_version INTEGER, 
	data_json JSON NOT NULL, 
	occurred_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_id, sequence), 
	FOREIGN KEY(case_id) REFERENCES cases (id)
);

CREATE INDEX ix_case_events_case_id ON case_events (case_id);

CREATE INDEX ix_case_events_source ON case_events (source_event_id);

CREATE TABLE approval_challenges (
	id VARCHAR(64) NOT NULL, 
	action_id VARCHAR(64) NOT NULL, 
	customer_id VARCHAR(64) NOT NULL, 
	payload_hash VARCHAR(80) NOT NULL, 
	case_version INTEGER NOT NULL, 
	expires_at VARCHAR(32) NOT NULL, 
	used_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(action_id) REFERENCES actions (id)
);

CREATE INDEX ix_approval_challenges_action_id ON approval_challenges (action_id);

CREATE TABLE approvals (
	id VARCHAR(64) NOT NULL, 
	action_id VARCHAR(64) NOT NULL, 
	approver_id VARCHAR(64) NOT NULL, 
	action_hash VARCHAR(80) NOT NULL, 
	scope VARCHAR(64) NOT NULL, 
	challenge_id VARCHAR(64) NOT NULL, 
	expires_at VARCHAR(32) NOT NULL, 
	revoked_at VARCHAR(32), 
	revocation_reason VARCHAR(256), 
	consumed_at VARCHAR(32), 
	created_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(action_id) REFERENCES actions (id)
);

CREATE INDEX ix_approvals_action_id ON approvals (action_id);

CREATE TABLE bank_instructions (
	id VARCHAR(64) NOT NULL, 
	plan_id VARCHAR(64) NOT NULL, 
	case_id VARCHAR(64) NOT NULL, 
	action_id VARCHAR(64) NOT NULL, 
	instruction_type VARCHAR(32) NOT NULL, 
	source_account_id VARCHAR(64) NOT NULL, 
	destination_account_id VARCHAR(64), 
	amount_minor INTEGER NOT NULL, 
	currency VARCHAR(3) NOT NULL, 
	effective_at DATE NOT NULL, 
	offer_id VARCHAR(64) NOT NULL, 
	product_version VARCHAR(32) NOT NULL, 
	term_days INTEGER, 
	apy_decimal VARCHAR(16) NOT NULL, 
	external_ref VARCHAR(64), 
	request_ref VARCHAR(80) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	reconciliation_json JSON, 
	created_at VARCHAR(32) NOT NULL, 
	updated_at VARCHAR(32) NOT NULL, 
	PRIMARY KEY (id), 
	FOREIGN KEY(plan_id) REFERENCES cash_plans (id), 
	FOREIGN KEY(case_id) REFERENCES cases (id), 
	FOREIGN KEY(action_id) REFERENCES actions (id)
);

CREATE INDEX ix_bank_instructions_case_id ON bank_instructions (case_id);
