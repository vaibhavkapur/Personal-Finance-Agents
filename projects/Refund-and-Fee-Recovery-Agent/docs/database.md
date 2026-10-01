---
title: "Database"
layout: default
nav_order: 6
---

# Database

The committed migrations and persistence modules below are the implementation reference. Runtime SQLite files, write-ahead logs, and generated keys are local state and are not committed.

## Schema files

- [migrations/001_initial.sql](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/migrations/001_initial.sql)

## Persistence implementation

- [backend/app/domain/models.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/domain/models.py)
- [backend/app/persistence/__init__.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/persistence/__init__.py)
- [backend/app/persistence/db.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/persistence/db.py)
- [backend/app/persistence/inbox.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/persistence/inbox.py)
- [backend/app/persistence/outbox.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/persistence/outbox.py)
- [backend/app/persistence/repositories.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Refund-and-Fee-Recovery-Agent/backend/app/persistence/repositories.py)

See [Architecture](architecture.md) for application/provider separation, aggregate storage, events, and reconciliation. Use the project setup command to create and seed local state; do not substitute an old runtime database for the fixtures.
