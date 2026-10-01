---
title: "Demo script"
layout: default
nav_order: 23
---



# Demo script

All three scenarios run with one command against a fresh in-process database:

```bash
cd backend && python scripts/demo.py            # all three
cd backend && python scripts/demo.py --scenario 2
```

The same scenarios can be walked through in the UI (`make api`, open http://localhost:8000). The operator page has the clock, worker and failure-mode controls; the customer page has the review screen and the assistant.

Fixture facts (synthetic): CD `cd_demo_1` $10,000 at Harbor matures 2026-10-03; checking has $1,200 available with tuition $1,200 due 2026-10-01 and rent $2,000 due 2026-10-05; the simulation clock starts 2026-09-26.

## Demo 1 — Maturity handled

1. Customer: *"My $10,000 CD matures next week. Keep $3,000 available for my upcoming expenses and compare what I can do with the rest."*
   The agent reads the snapshot, opens the case with a $3,000 reserve and the rent obligation, evaluates, and asks two questions the records cannot answer (lock-up preference; whether the $3,000 already includes the rent). It also warns that checking falls to $0 on 10-01 before maturity — the locked CD cannot fund tuition.
2. Customer: *"Yes, the $3,000 includes the rent. I can lock money up for a year."*
   The engine enforces a $1,000 floor (not $3,000 + $2,000): up to **$7,000** can be placed. Options over a common 365-day horizon: 12-month renewal 4.10% → $287.00; HYSA transfer 3.90% variable → $273.00; 6-month renewal 3.85% simple → $134.38; Meridian 5.00% promo excluded (`eligibility_unknown`). Each cites provider, product version and retrieval time.
3. Customer: *"Renew into the 12-month CD."* → proposed action, `awaiting_approval`, payload hash shown.
4. Customer: *"Approve it now."* → refused; the agent cannot approve.
5. Customer approves on the review screen (challenge + approve bound to the hash and case version).
6. Worker: re-verifies authority, refreshes balance and offer, persists `submitting`, submits once → bank accepts (`bankref_…`), case `submitted`.
7. Clock → 2026-10-03: the bank matures the CD, opens the renewed $7,000 deposit, pays $3,000 to checking and calls back. Worker verifies: 11/11 reconciliation checks match → `completed` with `provider_reference:bankref_…`.

## Demo 2 — Offer changes

Steps 1–5 as above, then the operator sets offer mode `changed_rate` (Harbor re-issues the 12-month product as `2026-09-r2-revised` at 3.75%). The worker detects the material change before submitting: approval invalidated, case `needs_requote`, no provider submission. The agent explains the change; *"Show me the revised offers"* re-evaluates with the new version; the customer selects and approves again; the journey completes with exactly one submission.

## Demo 3 — Uncertain transfer

The customer keeps the funds liquid and moves $7,000 to Northwind savings. The operator sets submit mode `accepted_before_timeout`: the bank records acceptance, then the connection drops. The executor marks `outcome_unknown`; the reconcile job looks up the original `request_ref`, finds the accepted instruction and moves the case to `submitted` without a second submission. At maturity the transfer is applied and verified (10/10 checks; savings +$7,000, checking +$3,000).

## Other failure modes to try

| Operator control | Expected outcome |
| --- | --- |
| submit mode `insufficient_available` / `declined` | case `rejected`, no funds moved |
| submit mode `malformed_response` | `outcome_unknown` → lookup → `submitted` |
| submit mode `accepted_not_effective` + advance 12 days | `manual_review`: accepted but not effective |
| submit mode `completed_amount_mismatch` | `manual_review`: reconciliation exception (`amount_minor`), case stays open |
| submit mode `delayed_callback` | verified by polling even though the callback is late |
| offer mode `expired_offer` | `needs_requote`: offer expired before execution |
| revoke access to `acct_cd_1` | reads and execution blocked → `manual_review` |
| forged provider event (bad signature) | stored, ignored; state unchanged |
| replay an inbox event | idempotent no-op |
| kill the worker between `submitting` and result | recovery looks up by `request_ref`; no duplicate (see `test_restart_mid_submission_creates_no_duplicate`) |
