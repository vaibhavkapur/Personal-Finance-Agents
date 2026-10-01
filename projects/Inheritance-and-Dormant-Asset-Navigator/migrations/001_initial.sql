-- Initial PostgreSQL schema generated from persistence metadata.

CREATE TABLE case_events (
	id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	sequence INTEGER NOT NULL, 
	sealed TEXT NOT NULL, 
	PRIMARY KEY (id), 
	UNIQUE (case_id, sequence)
)

;

CREATE TABLE cases (
	id VARCHAR NOT NULL, 
	tenant VARCHAR NOT NULL, 
	version INTEGER NOT NULL, 
	sealed TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;

CREATE TABLE event_inbox (
	provider VARCHAR NOT NULL, 
	event_id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	PRIMARY KEY (provider, event_id)
)

;

CREATE TABLE jobs (
	id VARCHAR NOT NULL, 
	case_id VARCHAR NOT NULL, 
	tenant VARCHAR NOT NULL, 
	action_id VARCHAR NOT NULL, 
	status VARCHAR NOT NULL, 
	lease_until VARCHAR, 
	attempts INTEGER NOT NULL, 
	error VARCHAR, 
	PRIMARY KEY (id), 
	UNIQUE (action_id)
)

;

CREATE TABLE provider_requests (
	request_ref VARCHAR NOT NULL, 
	payload_hash VARCHAR NOT NULL, 
	case_ref VARCHAR NOT NULL, 
	sealed TEXT NOT NULL, 
	PRIMARY KEY (request_ref), 
	UNIQUE (case_ref)
)

;

CREATE TABLE settings (
	id VARCHAR NOT NULL, 
	value TEXT NOT NULL, 
	PRIMARY KEY (id)
)

;