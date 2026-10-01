-- Generated from SQLAlchemy metadata. scripts.migrate initializes schema idempotently.

CREATE TABLE actions (
	id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	customer_id VARCHAR NOT NULL, 
	kind VARCHAR, 
	payload JSON, 
	payload_hash VARCHAR, 
	status VARCHAR, 
	idempotency_key VARCHAR, 
	challenge_id VARCHAR, 
	expires_at VARCHAR, 
	approval_id VARCHAR, 
	provider_ref VARCHAR, 
	result JSON, 
	PRIMARY KEY (id), 
	UNIQUE (customer_id, idempotency_key)
)

;

CREATE TABLE approvals (
	id VARCHAR NOT NULL, 
	action_id VARCHAR, 
	approver VARCHAR, 
	payload_hash VARCHAR, 
	expires_at VARCHAR, 
	revoked_at VARCHAR, 
	consumed_at VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (action_id)
)

;

CREATE TABLE case_events (
	id VARCHAR NOT NULL, 
	case_id VARCHAR, 
	sequence INTEGER, 
	event_type VARCHAR, 
	actor VARCHAR, 
	occurred_at VARCHAR, 
	previous_state VARCHAR, 
	next_state VARCHAR, 
	data JSON, 
	PRIMARY KEY (id), 
	UNIQUE (case_id, sequence)
)

;

CREATE TABLE financial_incidents (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	discovered_at VARCHAR, 
	data JSON NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE mock_provider_actions (
	request_ref VARCHAR NOT NULL, 
	provider_id VARCHAR, 
	payload_hash VARCHAR, 
	result JSON, 
	PRIMARY KEY (request_ref)
)

;

CREATE TABLE outbox_jobs (
	id VARCHAR NOT NULL, 
	action_id VARCHAR, 
	status VARCHAR, 
	priority INTEGER, 
	lease_until VARCHAR, 
	attempts INTEGER, 
	last_error VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (action_id)
)

;

CREATE TABLE provider_event_inbox (
	id VARCHAR NOT NULL, 
	provider_id VARCHAR, 
	event_id VARCHAR, 
	case_id VARCHAR, 
	payload JSON, 
	PRIMARY KEY (id), 
	UNIQUE (provider_id, event_id)
)

;

CREATE TABLE sessions (
	token_hash VARCHAR NOT NULL, 
	customer_id VARCHAR, 
	expires_at VARCHAR, 
	PRIMARY KEY (token_hash)
)

;

CREATE TABLE settings (
	key VARCHAR NOT NULL, 
	value JSON, 
	PRIMARY KEY (key)
)

;

CREATE TABLE tool_runs (
	id VARCHAR NOT NULL, 
	case_id VARCHAR, 
	name VARCHAR, 
	occurred_at VARCHAR, 
	outcome VARCHAR, 
	metadata JSON, 
	PRIMARY KEY (id)
)

;
