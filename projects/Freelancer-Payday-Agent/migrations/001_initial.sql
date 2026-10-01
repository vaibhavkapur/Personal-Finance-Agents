PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS entities (
  kind TEXT NOT NULL, id TEXT NOT NULL, tenant TEXT NOT NULL, customer TEXT NOT NULL,
  data TEXT NOT NULL CHECK(json_valid(data)), PRIMARY KEY(kind,id)
);
CREATE INDEX IF NOT EXISTS entity_scope ON entities(tenant,customer,kind);
CREATE UNIQUE INDEX IF NOT EXISTS action_idempotency ON entities(tenant,json_extract(data,'$.idempotency_key')) WHERE kind='actions';
CREATE UNIQUE INDEX IF NOT EXISTS transfer_reference ON entities(json_extract(data,'$.request_ref')) WHERE kind='payday_transfers';
CREATE TABLE IF NOT EXISTS case_events (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, case_id TEXT NOT NULL, sequence INTEGER NOT NULL,
 event_type TEXT NOT NULL, previous_state TEXT, next_state TEXT NOT NULL, actor TEXT NOT NULL,
 occurred_at TEXT NOT NULL, expected_version INTEGER NOT NULL, detail TEXT NOT NULL,
 UNIQUE(case_id,sequence)
);
CREATE TABLE IF NOT EXISTS bucket_journals (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, customer TEXT NOT NULL,
 idempotency_key TEXT UNIQUE NOT NULL, reference_type TEXT NOT NULL, reference_id TEXT NOT NULL,
 posted_at TEXT NOT NULL, lines TEXT NOT NULL CHECK(json_valid(lines))
);
CREATE TABLE IF NOT EXISTS outbox (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, action_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL DEFAULT 'pending', lease_until REAL NOT NULL DEFAULT 0,
 attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT
);
CREATE TABLE IF NOT EXISTS event_inbox (
 provider TEXT NOT NULL, event_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
 payload TEXT NOT NULL, received_at TEXT NOT NULL, PRIMARY KEY(provider,event_id)
);
CREATE TABLE IF NOT EXISTS tool_runs (
 id TEXT PRIMARY KEY, tenant TEXT NOT NULL, case_id TEXT NOT NULL, tool TEXT NOT NULL,
 occurred_at TEXT NOT NULL, outcome TEXT NOT NULL, detail TEXT NOT NULL
);
