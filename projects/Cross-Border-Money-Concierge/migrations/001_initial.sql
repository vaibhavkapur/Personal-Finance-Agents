-- Initial schema. Runtime creates identical tables using SQLAlchemy metadata.

CREATE TABLE actions (
	id VARCHAR NOT NULL, 
	case_id VARCHAR, 
	customer_id VARCHAR, 
	status VARCHAR, 
	idem_key VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (customer_id, idem_key)
)

;

CREATE TABLE approvals (
	id VARCHAR NOT NULL, 
	action_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (action_id)
)

;

CREATE TABLE beneficiaries (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR NOT NULL, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE case_events (
	id VARCHAR NOT NULL, 
	case_id VARCHAR, 
	sequence INTEGER, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_id, sequence)
)

;

CREATE TABLE cases (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR NOT NULL, 
	status VARCHAR, 
	version INTEGER, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE delivery_receipts (
	id VARCHAR NOT NULL, 
	transfer_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (transfer_id)
)

;

CREATE TABLE documents (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE mock_provider_operations (
	id VARCHAR NOT NULL, 
	request_ref VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (request_ref)
)

;

CREATE TABLE mock_provider_transfers (
	id VARCHAR NOT NULL, 
	request_ref VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (request_ref)
)

;

CREATE TABLE outbox_jobs (
	id VARCHAR NOT NULL, 
	case_id VARCHAR, 
	status VARCHAR, 
	due_at VARCHAR, 
	lease_until VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE provider_event_inbox (
	id VARCHAR NOT NULL, 
	provider_id VARCHAR, 
	event_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (provider_id, event_id)
)

;

CREATE TABLE provider_requirements (
	id VARCHAR NOT NULL, 
	transfer_id VARCHAR, 
	provider_request_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (provider_request_id)
)

;

CREATE TABLE remittance_quotes (
	id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE remittance_transfers (
	id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	request_ref VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_id), 
	UNIQUE (request_ref)
)

;

CREATE TABLE sessions (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE settings (
	id VARCHAR NOT NULL, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE tool_runs (
	id VARCHAR NOT NULL, 
	customer_id VARCHAR, 
	case_id VARCHAR, 
	data TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;