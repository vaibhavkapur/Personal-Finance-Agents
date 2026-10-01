---
title: "Architecture"
layout: default
nav_order: 3
---

# Architecture

## Application flow

The React interface calls FastAPI in `backend/app/api/main.py`. `scripts/run.py` starts the API on port 8017 and a separate durable worker. `WealthService` in `backend/app/workflows/service.py` owns case transitions, goal revisions, proposals, approval, provider submission, and reconciliation.

## Deterministic planning

`backend/app/domain/engine.py` calculates goal horizons, account values, funding gaps, and constrained baskets. It validates prices, allocations, and mandate bounds before a proposal can execute. Goals, account holdings, contribution scenarios, and the replay clock start from synthetic versioned fixtures.

## Durable state and providers

`backend/app/persistence/store.py` stores the versioned customer aggregate plus events, outbox records, and leased jobs in SQLite. `backend/app/adapters/mock.py` implements an independent SQLite-backed custodian and a provider interface with preview, submit, and lookup operations. Partial execution is reconciled before a new remainder proposal is constructed; previously completed actions are not submitted again.

## Authority and tools

Customer reviews bind proposals to their version, payload hash, and challenge. Goal or mandate changes invalidate affected approvals. `backend/app/agent/tools.py` implements scoped typed tools and a rules-only planner; it cannot approve a proposal or mutate financial state just by explaining a request. The API also exposes an MCP HTTP endpoint at `/mcp`.

## Prototype scope

The application uses synthetic portfolios and mock provider execution. It has no live custodian, external model, or production identity integration. The default customer session is a local demo facility. `frontend/src` contains the actual customer and operator screens; the API serves the production frontend build.
