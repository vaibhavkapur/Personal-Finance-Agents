---
title: "Database"
layout: default
nav_order: 6
---

# Database

The committed migrations and persistence modules below are the implementation reference. Runtime SQLite files, write-ahead logs, and generated keys are local state and are not committed.

## Schema files

- [migrations/0001_initial.sql](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/migrations/0001_initial.sql)

## Persistence implementation

- [backend/app/persistence/__init__.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/backend/app/persistence/__init__.py)
- [backend/app/persistence/db.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/backend/app/persistence/db.py)
- [backend/app/persistence/models.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/backend/app/persistence/models.py)
- [backend/app/persistence/seed.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/backend/app/persistence/seed.py)

See [Architecture](architecture.md) for application/provider separation, aggregate storage, events, and reconciliation. Use the project setup command to create and seed local state; do not substitute an old runtime database for the fixtures.
