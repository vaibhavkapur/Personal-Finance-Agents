---
title: "API Reference"
layout: default
nav_order: 4
---

# API Reference

The implemented FastAPI schema is served at [http://127.0.0.1:8017/docs](http://127.0.0.1:8017/docs) when the app is running.

The route inventory below is extracted from the application source. Use the running OpenAPI page for request schemas, response models, and parameter details. Routes listed here are the actual decorator paths; any router prefix is defined at the linked source.

## backend/app/api/main.py

[Route source](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/backend/app/api/main.py)

- `POST /v1/session` — `login`
- `GET /health` — `health`
- `GET /v1/portfolio` — `portfolio`
- `POST /v1/wealth-cases` — `create_case`
- `GET /v1/wealth-cases/{case_id}/goal-status` — `goal_status`
- `POST /v1/wealth-cases/{case_id}/scenarios` — `preview`
- `POST /v1/wealth-cases/{case_id}/goals` — `revision`
- `POST /v1/wealth-cases/{case_id}/rebalance-proposals` — `proposal`
- `POST /v1/actions/{proposal_id}/approve` — `approve`
- `POST /v1/actions/{proposal_id}/reject` — `reject`
- `POST /v1/mandates/preview` — `mandate`
- `POST /v1/mandates/approve` — `mandate_approve`
- `POST /v1/simulator/configure` — `configure`
- `POST /v1/simulator/settle` — `settle`
- `POST /v1/simulator/tick` — `tick`
- `POST /v1/agent/messages` — `chat`
- `POST /v1/provider-events/custodian` — `provider_event`
- `POST /mcp` — `mcp`
- `GET /favicon.svg` — `favicon`
- `GET /{path:path}` — `frontend`
