---
title: "Database"
layout: default
nav_order: 6
---

# Database

The committed migrations and persistence modules below are the implementation reference. Runtime SQLite files, write-ahead logs, and generated keys are local state and are not committed.

## Schema files

- [migrations/001_initial.postgresql.sql](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/migrations/001_initial.postgresql.sql)

## Persistence implementation

- [backend/app/persistence/store.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/backend/app/persistence/store.py)

See [Architecture](architecture.md) for application/provider separation, aggregate storage, events, and reconciliation. Use the project setup command to create and seed local state; do not substitute an old runtime database for the fixtures.
