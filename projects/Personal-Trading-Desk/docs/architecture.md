---
title: "Architecture"
layout: default
nav_order: 3
---

# Architecture

## Application flow

The React desk calls FastAPI in `backend/app/main.py`. The API creates an `Engine` that coordinates mandate evaluation, risk checks, action approval, durable jobs, and the mock broker. The default API process runs an embedded worker; a separate worker entry point is also available in `backend/app/worker.py`.

## Domain and execution

`backend/app/domain/market.py` evaluates replayed market snapshots within the fixture trading session. `domain/risk.py` checks mandates, quote freshness, cash, open reservations, existing positions, and exposure before an order is prepared. The workflow engine binds approval to a payload hash, challenge, and order version and looks up the original broker reference when an outcome is uncertain.

`backend/app/adapters/paper.py` defines the paper broker interface and independent SQLite-backed mock broker. Partial fills and cancellation races update the original order; reconciliation compares application positions and cash with broker evidence. `backend/app/persistence/store.py` stores entities, events, jobs, and tenant-scoped records. The SQLite schema is in `migrations/001_initial.sql`.

## Agent and boundaries

`backend/app/agent/tools.py` exposes typed tools and a bounded explanation policy. `backend/app/agent/mcp.py` exposes the same domain through local stdio MCP. The model-facing tool interface cannot approve actions. The application rejects a non-mock `TRADING_ENVIRONMENT`; there is no live broker connection or real-money execution.

## User interface

`frontend/src` contains the React customer and operations views. The API serves `frontend/dist` after the production build. The development proxy targets API port 8000. The fixture login password defaults to `paper-demo` and is only for this local synthetic workspace.
