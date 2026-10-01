---
title: "Implementation Guide"
layout: default
nav_order: 10
---

# Implementation Guide

The detailed implementation guide below was imported with the application. Previously recorded evaluation and Docker/browser results retain their original scope; see [Testing](testing.md) for the checks rerun during this publication.

# Tax-Season Concierge

A runnable 2025 US federal tax-preparation prototype: explicit eligibility, synthetic document intake, duplicate/W-2c reconciliation, deterministic IRS Tax Table calculations, line-level evidence, exact-package approval, durable mock filing, and separate refund matching.

**All taxpayer data is synthetic. No live tax return, bank payment, or refund-account change is possible.** The demo session identifies a private synthetic tenant; it is not production identity verification. The rules have automated source verification, not independent professional tax review.

## Run

Python 3.11+ and Node 20.19+ are required for native development. Docker is the easiest full-stack path:

```sh
docker compose up --build -d
```

Open **http://127.0.0.1:8096**. The stack runs PostgreSQL, FastAPI, one durable worker, and a separate credential-protected mock filing service. Only the app and development database ports bind to localhost. Credentials in Compose are public synthetic-demo fixtures.

For the single-process local development path (SQLite and in-process simulator):

```sh
./scripts/run.sh
```

Set `PYTHON=/path/to/python3.12` on the first run if your system Python is older. Once dependencies and the frontend are built:

```sh
PYTHONPATH=backend .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8096
```

The API serves the built React app. SQLite state lives in ignored `.data/tax.db`; Compose state lives in the `tax-season-concierge_tax-data` volume. `docker compose down` stops containers without deleting case data. API documentation is at `/docs`.

## Use the app

1. Confirm the synthetic profile. **Use sample answers** fills the fixture questionnaire; **Confirm supported profile** records your attestation.
2. Open Documents, import the sample records, review fields, and confirm completeness. Missing forms never imply zero income.
3. Prepare the draft. Click any worksheet line to inspect sources and the pinned rule reference. Download the complete JSON package or open the print-friendly worksheet.
4. Review the exact destination, documents, amounts and content hash. Explicitly approve one mock submission.
5. In Filing & refund, advance the simulation clock. Receipt, acceptance, refund notice and matched account credit remain separate. A balance-due case stops at follow-up and never pretends a payment occurred.

Use **New sample case** for missing interest, corrected withholding, balance due, identity rejection, delayed outcomes, a malformed response or a timeout after provider acceptance. Operator view exposes action authority, stable request references, event delivery counts and status replay for held uncertain submissions.

## Repeatable demos

With the app running, each command creates a new isolated synthetic session and performs explicit fixture approval:

```sh
.venv/bin/python scripts/demo.py --scenario two_jobs
.venv/bin/python scripts/demo.py --scenario corrected_form
.venv/bin/python scripts/demo.py --scenario balance_due
```

The same command supports `missing_interest`, `rejection`, `timeout`, `malformed` and `delayed`. These scripts are demonstrations of the simulator, not customer authorizations or real external actions.

## Reference result

The two-job fixture has wages of $42,000.25 and $30,000.25, taxable bank interest of $245.75, and W-2 federal withholding of $4,800.50 and $3,400.00. The calculator sums cents per line before rounding: wages $72,001; interest $246; total income $72,247; standard deduction $15,750; taxable income $56,497. The official single-filer Tax Table row is $56,450–$56,500 with tax of $7,339. Withholding rounds to $8,201, producing a potential refund of **$862**.

The corrected W-2c fixture replaces only Northstar withholding and produces **$1,362** potential refund. The balance-due fixture produces **$2,339 due**, excluding any penalty or accrued interest.

## Checks

```sh
.venv/bin/pytest
PYTHONPATH=backend .venv/bin/python scripts/evaluate.py
.venv/bin/python scripts/pin_rules.py
npm run build --prefix frontend
npm run test:e2e --prefix frontend
```

Measured results: **61 backend tests**, **43/43 labeled evaluations**, **2,062 source-verified tax rows**, **all eight HTTP demos**, and **one complete desktop/mobile browser journey** passed. The four-service Docker deployment was built and verified.

The browser test expects a running app and Chrome. On macOS it uses the installed Google Chrome by default; set `CHROME_PATH` for another executable, or configure Playwright's installed Chromium. The scenario report is `docs/evaluation.json`: 43 labeled fixtures, including 10 held-out fixtures, compared with a direct rules-only baseline. This is synthetic regression coverage, not a measurement of an LLM or real filing accuracy. The independently prepared tax-review release gate remains open.

Python dependencies are pinned in `requirements.lock`; frontend dependencies are pinned in `frontend/package-lock.json`. The five MCP tools use the 2025-06-18 stdio protocol. No external model SDK or credential is required. See `docs/mcp.md` for integration.

## Scope and boundaries

Only tax year 2025, US federal, single US-resident adults under 65, not blind or dependent, standard deduction, ordinary W-2 wages and domestic bank interest are supported. Interest above $1,500, other Schedule B conditions, taxable income at or above $100,000, possible earned income credit, unsupported deductions, credits, income or schedules stop preparation.

Document intake accepts **synthetic structured JSON**, not arbitrary PDF/OCR or real customer records. Fields require integer cents. Original extraction, correction chains and confirmation evidence are retained. Identifiers must have a synthetic prefix. The print-friendly worksheet and JSON review package are not certified e-file payloads.

The prototype ships a bounded deterministic concierge and a structured model interface, with five MCP tools. A hosted LLM, A2A, sandbox government provider, encrypted real-customer document vault, identity proofing, production user roles and live filing are intentionally absent. PostgreSQL uses serialized transactions for this small prototype; large-scale deployment would need row-level contention management and operational hardening.

## Project map

- `backend/app/domain`: eligibility, reconciliation, integer-money calculator and pinned-source checks.
- `backend/app/workflows`: transitions, immutable approvals, leased jobs, replay and evidence.
- `backend/app/adapters`: in-process simulator, HTTP adapter and standalone provider service.
- `backend/app/agent`: scoped tools, stdio MCP, optional structured-model interface.
- `frontend`: React + TypeScript customer and operator views.
- `rules/2025`: archived official sources, checksums and 2,062 extracted single-filer tax rows.
- `fixtures`: synthetic input documents and labeled evaluation cases.
- `docs`: original plan, architecture, API contract, demo instructions, test results and screenshots.

See `docs/architecture.md` for trade-offs, `docs/api.md` for endpoint semantics, and `docs/test-report.md` for measured verification and remaining release gates.
