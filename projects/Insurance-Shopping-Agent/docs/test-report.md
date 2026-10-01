---
title: "Test report (fixture version 2026.09.1)"
layout: default
nav_order: 25
---



# Test report (fixture version 2026.09.1)

Measured on 2026-09-26 with Python 3.9.6, SQLite, direct and in-process A2A adapters. Re-run with `make test` and `make eval`.

## pytest

`50 passed` (`tests/`). Coverage of plan §19 "domain and failure cases":

| # | Case | Test |
|---|---|---|
| 1 | Unknown underwriting answer never becomes false/no | `test_domain.py::test_case1_*` (two tests), `test_api_flows.py::test_demo2_*` |
| 2 | Cheaper quote missing required coverage is excluded | `test_domain.py::test_case2_*`, `test_api_flows.py::test_demo1_*`, `test_unsuitable_quote_cannot_be_applied_for` |
| 3 | Different deductibles displayed | `test_domain.py::test_case3_*`, `test_ranking_respects_lower_deductible_preference_*` |
| 4 | One insurer times out, two valid quotes remain | `test_api_flows.py::test_case4_*` |
| 5 | Revised premium / material change invalidates approval | `test_demo3_revised_terms_require_fresh_approval`, `test_case5_*`, `test_case5b_*` |
| 6 | A2A task completing with a quote ≠ issued policy | `test_adapters.py::test_case6_*`, `test_a2a_flow.py` |
| 7 | Document instructions ignored by tools | `test_agent.py::test_case7_*`, `test_mcp_server_handshake_list_and_call` |
| 8 | Issued effective date differs → verification fails | `test_domain.py::test_case8_*`, `test_api_flows.py::test_case8_*` |

Additional: approval edge cases (stale version 409, changed payload 409, forged challenge 409, expired challenge 410), idempotency-key reuse, tenant isolation, uncertain write reconciliation with exactly one provider submit, signed provider events with dedupe and replay, quote expiry, operator metrics and review resolution, worker crash/lease recovery, two workers racing, tool-budget escalation, contradiction escalation.

## Labeled evaluation set (`fixtures/eval_cases.json`)

33 cases in five categories; 9 held out.

```
Rules-only workflow:  33/33 passed (held-out 9/9); unsupported claims 0; unnecessary questions 0; provider calls 162; duplicate submits 0
Agent (scripted):     11/11 passed; tool runs 56; provider calls 63; unnecessary questions 0
  conflicting_evidence   4/4
  missing_information    7/7
  ordinary_completion    8/8
  refusal                6/6
  uncertain_provider     8/8
```

Definitions: *unsupported claims* = comparison values or failed checks on cited fields without a clause reference; *unnecessary questions* = application questions surfaced for insurers whose quote is excluded (the metric drove a change: application questions are now only surfaced once an insurer has quoted and is not excluded); *duplicate submits* = provider `submit_application` calls beyond executed actions. Token cost is not measured for the scripted provider; tool runs per case are recorded in `tool_runs`.

## Release gates (plan §19)

| Gate | Status |
|---|---|
| All fixture suitability decisions match the labeled interpretation | met (33/33) |
| Every material comparison field has a policy or quote reference | met (`unsupported_claims_total == 0`, asserted in `test_eval.py`) |
| All changed underwriting offers require a fresh customer decision | met (Demo 3 tests, `eval_018`, `eval_019`) |
| No unapproved external writes, cross-customer access or duplicated side effects | met (executor authority tests, tenant tests, `duplicate_submits == 0`, worker race test) |
| Every claimed external completion has a provider/evidence reference | met (`completion_evidence_ref` = issued policy id with declarations document) |
| Restart the worker mid-case without losing approvals, timers or evidence | met (`test_worker_recovery.py`) |

These are results against simulators and fixtures, not customer or market outcomes.
