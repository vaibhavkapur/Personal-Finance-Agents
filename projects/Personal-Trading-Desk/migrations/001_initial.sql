-- SQLite local prototype. BEGIN IMMEDIATE serializes risk checks + reservations.
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
INSERT OR IGNORE INTO schema_migrations VALUES(1);
CREATE TABLE IF NOT EXISTS event_inbox(provider TEXT, event_id TEXT, received_at TEXT, PRIMARY KEY(provider,event_id));
CREATE TABLE IF NOT EXISTS idempotency(tenant TEXT, scope TEXT, key TEXT, payload_hash TEXT, result_id TEXT, PRIMARY KEY(tenant,scope,key));
CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, action_id TEXT UNIQUE NOT NULL, state TEXT NOT NULL DEFAULT 'pending', lease_until REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS case_events(seq INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, tenant TEXT NOT NULL, case_id TEXT, event_type TEXT NOT NULL, actor TEXT NOT NULL, occurred_at TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, delivered_at TEXT);
