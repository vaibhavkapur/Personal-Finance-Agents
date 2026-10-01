---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Insurance Claims Advocate

A delayed-baggage **claims advocate** prototype. It turns source-linked travel documents into a reviewable claim packet, follows a mock insurer’s requests, explains the decision against a fixture policy, and reconciles any simulated payout before closure.

This is a working US-first prototype against **synthetic records and a mock insurer**. Nothing is filed with a real carrier. Dollar amounts, deadlines, and provider names are fixtures unless a source says otherwise. Recovery is never guaranteed; a supported rejection is a valid outcome.

> Built a persistent insurance-claims prototype that turns source-linked documents into approved submissions, resolves insurer evidence requests and verifies decisions and payouts through a replay-safe workflow.

## What you can demonstrate

1. **Complete claim** — prepare and submit a supported packet, then match the mock payout.
2. **Missing evidence** — the insurer requests arrival confirmation; the same case resumes after upload.
3. **Partial rejection** — accepted and excluded items are explained; a challenge is drafted only where evidence warrants it.

**MVP acceptance:** a claim is submitted once, missing evidence is resolved, the insurer decision is explained with policy references, and any simulated payout is matched to the accepted claim before closure.

## Quick start (local, no credentials)

Python 3.9+ is enough. SQLite is the default store.

```bash
cd Insurance-Claims-Advocate
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python scripts/seed.py
uvicorn app.main:create_app --factory --app-dir backend --host 127.0.0.1 --port 8000
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000). Sign in as **Jordan Rivera** (fixture token `tok_customer_demo_3`).

Fixture identities (never real credentials):

| Token | Role |
|---|---|
| `tok_customer_demo_3` | Jordan Rivera (claimant) |
| `tok_rep_demo_3` | Authorized representative for Jordan |
| `tok_customer_demo_4` | Priya Natarajan (other customer; used to prove isolation) |
| `tok_operator_demo` | Operations reviewer |

After you approve a packet, switch to the **Operator** tab (as the operations reviewer) and click **Run worker until idle**. Submissions are executed by the worker, not by the approval itself.

## Documented end-to-end demo

```bash
python scripts/demo.py              # all three scenarios
python scripts/demo.py complete
python scripts/demo.py missing-evidence
python scripts/demo.py partial-rejection
```

Walkthrough: [docs/demo.md](demo.md).

## Tests and evaluation

```bash
python -m pytest tests/ -q
python scripts/run_evals.py         # 33 labeled cases, agent vs rules-only
```

Latest measured results (synthetic fixtures only): [docs/eval-report.md](eval-report.md). On 26 September 2026 the suite reported **33/33** for both the scripted agent and the rules-only baseline, including the 7 held-out cases. Those figures are prototype regression results, not customer or market outcomes.

Release-gate coverage in the regression suite includes: fixture totals to the cent, provenance on submitted facts, incomplete evidence staying open, no unapproved writes, no cross-customer access, no duplicated side effects, required provider references, and worker restart mid-case.

## Docker (API + worker + Postgres)

```bash
docker compose up --build
```

Postgres is the source of truth in Compose. Redis is not required. The mock insurer and A2A insurer agent run in-process on the API.

## How the product is structured

```text
Customer / reviewer
  -> Web app and authenticated API
  -> Agent orchestrator + durable case state
  -> Typed tools and deterministic domain engine
  -> Policy check and exact-action approval
  -> ClaimsAdapter
  -> Mock / sandbox provider
  -> Status events, verification and evidence timeline
```

| Layer | Responsibility |
|---|---|
| Evidence intake | Immutable document versions, duplicate detection, dates/amounts/identity with locators |
| Policy interpreter | Loss-date policy version + reviewer-approved rules; ambiguity stays visible |
| Claim calculator | Eligible / excluded / uncertain / duplicate expenses; decimal (integer minor-unit) arithmetic |
| Case coordinator | Insurer requests, packet revisions, approvals, provider references |
| Decision & payout verifier | Explains items, drafts evidence-backed appeals, reconciles payment |

Calculations, authorization, eligibility and state transitions run in application code. The model only chooses tools and asks for missing information. It cannot approve, change permissions, or mark a case completed.

Docs:

- [Architecture](architecture.md)
- [Application API](api.md)
- [Provider capability matrix](capabilities.md)
- [Trade-offs](tradeoffs.md)

## Fixture policy (sample terms)

`travel_policy_demo_1` is a fictional US travel policy. Version **2026-09** (loss on 10 September 2026) reimburses essential clothing/toiletries after a **12-hour** checked-bag delay, up to **$120**, with a 90-day filing window and a 60-day written-review process. Version **2026-01** (August losses) uses a 24-hour threshold and a **$100** cap. The calculator uses the version that applies to the **loss date**, not the latest sample.

Labeled example: receipts $35 + $45 + $70 eligible, $40 excluded, cap $120 → supported $150, estimated payable $120. The insurer still decides the claim.

## Repository layout

```text
insurance-claims-advocate/
  backend/app/
    api/              # customer, operator and provider-facing endpoints
    agent/            # prompts, typed tools, MCP server, planners
    domain/           # facts, policy rules, packets, decisions, settlement
    adapters/         # mock insurer, mock payments, sandbox, A2A client
    workflows/        # state machine, approvals, executor, worker, outbox
    persistence/      # SQLAlchemy models, seed
  frontend/           # customer journey and operations view
  fixtures/           # synthetic people, documents, policy, eval cases
  tests/
  migrations/
  docs/
  scripts/demo.py     # three documented scenarios
  scripts/seed.py
  scripts/worker.py
  docker-compose.yml
```

## Stack notes

- **API / domain:** Python, FastAPI, Pydantic v2, SQLAlchemy 2.
- **Store:** SQLite locally; PostgreSQL in Compose. Provider calls stay outside database transactions: persist a pending action, then reconcile the result.
- **Worker:** persisted jobs with leases (`scripts/worker.py`). Temporal is not required.
- **UI:** customer and operator views served by the API. The prototype ships as a small TypeScript-free SPA so the demo runs with Python only. The surfaces match the plan (checklist, itemized amounts, approval review, timeline, operator adapter/job views).
- **Agent:** scripted planner by default (`AGENT_PLANNER=scripted`). Optional OpenAI-compatible LLM via `AGENT_PLANNER=llm` plus `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL`.
- **Protocols:** MCP tools over stdio (`python -m app.agent.mcp_server`, revision 2025-06-18). Optional A2A insurer agent at `/insurer-agent` (protocol 0.3, domain schema `claims-advocate/a2a-claims/1.0`). AP2 is not used; an insurer payout is not a customer checkout.

Pinned install versions live in `requirements.txt`.

## What this does not do

Medical claims, emergency assistance, litigation, multi-policy coordination, autonomous legal representation, fabricated receipts, live claim portals, or actual insurance payouts. Documents and provider messages are untrusted: instructions embedded in them never change tools or payout details.
