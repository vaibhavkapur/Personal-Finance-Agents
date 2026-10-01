---
title: "Demo script"
layout: default
nav_order: 23
---



# Demo script

One command runs everything in-process (no server, no credentials):

```bash
make demo          # direct adapters
make demo-a2a      # in-process A2A insurer agents over ASGI
python scripts/demo.py --base-url http://localhost:8000   # against a running stack
```

Fixture clock starts at 2026-10-01T09:00Z; the desired coverage start is 2026-11-01.

## Demo 1 — Comparable shopping (Avery Demo, `cus_demo_1`)

1. Case created with product, state, start date, limits and replacement-cost requirement → `collecting`, missing `address`, `deductible_preference`.
2. Customer message: *"Find renters insurance that covers replacing my belongings, includes liability protection and starts when I move next month."* Agent calls `get_confirmed_needs` and asks only for the missing fields.
3. Interview form: address, deductible cap $1,000, must cover jewelry + bicycles.
4. Agent calls `request_quotes`: Northwind and Harborline quote; Cedar & Pine returns `input_required` with its own wording: *"Do you own jewelry, watches or furs with a combined value over $1,500?"* (→ Demo 2).
5. After the answer, `compare_coverage`:
   - Suitable: Northwind $180 (deductible $1,000), Harborline $210 (deductible $500).
   - Excluded: Cedar & Pine $165 — cheapest, but jewelry excluded (`CP-EX-4`).
   - Trade-off text and the ranking disclosure ("product rule, not an actuarial evaluation").
6. Customer answers Northwind's four questions (verbatim wording), says `select <quote_id>`; agent calls `prepare_application` → review screen with destination, environment/protocol, premium, limits, deductible, start date, exclusions, the exact answers being sent, irreversible effects, payload hash.
7. Customer approves via `POST /v1/actions/{id}/approve` (challenge + hash + case version). Worker executes → `submitted` → `underwriting`. Case view shows `policy: null`: a submission is not a policy.
8. Clock +2h, worker polls → `bound` → `issued` → verification passes → `completed`. Timeline shows separate `quoted`, `approved by customer`, `submitted`, `bound`, `issued`, `verified` and `effective (coverage starts 2026-11-01)` labels. Agent: *"Coverage starts 2026-11-01; it is not in force before that date."*

## Demo 2 — Follow-up question

Embedded in Demo 1 step 4–5. `tests/test_api_flows.py::test_demo2_follow_up_question_resumes_insurer_task` also shows that answering `unknown` keeps the task blocked and that the same external task id resumes.

## Demo 3 — Changed terms (Jordan Sample, `cus_demo_2`, prior claim)

1. Same interview; Northwind quotes $180 and the customer approves the application.
2. Underwriting applies fixture rule `NW-UW-2` (prior-claim surcharge): the poller receives `revised_offer`, stores quote v2 at $216, builds application revision 2, invalidates the earlier action, and proposes `accept_revised_offer`. Event `insurance.application.revised_offer` carries `requires_new_approval: true`.
3. Review screen shows `$180.00 → $216.00 (+$36.00)` and the reason. Agent explains the change and that it cannot approve.
4. Customer approves the new terms; clock +1h; policy issued at $216 and verified → `completed`.

## Extra — Issued-policy mismatch

Fault `effective_date_shift_days=7` on Northwind. Policy is issued for 2026-11-08; verification fails on `effective_date`; case goes to `manual_review`, never `completed`; agent refuses to present coverage as active; operator view shows the mismatch event and redacted evidence.

## Multi-process A2A run

```bash
make insurers                      # three agents on :9001-:9003
ADAPTER_MODE=a2a make api          # API on :8000 with embedded worker
python scripts/demo.py --base-url http://localhost:8000
```

`docker compose up` runs the same stack with PostgreSQL, a separate worker container, and the Vite frontend on :5173.
