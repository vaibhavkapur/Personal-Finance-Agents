---
title: "API Reference"
layout: default
nav_order: 4
---

# API Reference

The implemented FastAPI schema is served at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs) when the app is running.

The route inventory below is extracted from the application source. Use the running OpenAPI page for request schemas, response models, and parameter details. Routes listed here are the actual decorator paths; any router prefix is defined at the linked source.

## backend/app/main.py

[Route source](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/backend/app/main.py)

- `GET /health` — `health`
- `POST /v1/session` — `login`
- `DELETE /v1/session` — `logout`
- `GET /v1/desk` — `desk`
- `POST /v1/evaluate` — `evaluate`
- `POST /v1/trading-runs` — `run`
- `POST /v1/trading-runs/{id}/evaluate` — `evaluate_case`
- `POST /v1/orders/preview` — `preview`
- `POST /v1/orders` — `prepare`
- `GET /v1/orders/{id}/executions` — `report`
- `POST /v1/orders/{id}/cancel-drafts` — `cancel`
- `POST /v1/orders/{id}/discard` — `discard`
- `POST /v1/actions/{id}/approve` — `approve`
- `POST /v1/trading-mandates` — `mandate`
- `POST /v1/trading-mandates/{id}/revoke` — `revoke`
- `POST /v1/controls/kill-switch` — `kill`
- `POST /v1/replay/advance` — `clock`
- `POST /v1/reconcile` — `reconcile`
- `POST /v1/worker/tick` — `tick`
- `POST /v1/agent/explain` — `explain`
- `POST /v1/provider-events/broker` — `callback`
- `GET /` — `index`
