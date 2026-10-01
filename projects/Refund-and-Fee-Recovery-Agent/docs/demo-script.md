---
title: "Demo script"
layout: default
nav_order: 23
---


# Demo script

Run everything in one command (in-memory, fixture clock, no network):

```bash
.venv/bin/python scripts/demo.py --write     # prints and writes docs/demo-output.md
```

Or drive it through the API and UI: start the API (`make api`), open the frontend (`make frontend`, needs Node), pick an order in the Recovery inbox, click **Ask the agent**, approve on the review screen, then use the Operator tab to run the worker and advance the fixture clock.

## Demo 1 — Missing refund recovered (`order_mock_499`)

1. Create the case for the $84.99 promise (`receipt_mock_1`, `promise_mock_1`). Note the promise document contains an attacker "reply-to" and an instruction to file an unauthorized-charge claim.
2. **Ask the agent.** It calls `get_recovery_status → find_refund_evidence → match_refund_credits → prepare_recovery_message`. Reconciliation rejects a same-amount credit from another merchant (`merchant_mismatch`) and the $59 refund that belongs to another order (`reference_belongs_to_other_order`). Nothing is sent.
3. **Review screen**: destination is `refunds@streambox.mock` from the merchant registry (not the attacker address), 3 attachments by hash, $84.99, irreversible-effect text. Approve.
4. Worker executes: `merchant_pending`, provider reference `mcase_…`, follow-up timer in 5 days.
5. Advance +1d (merchant "processing"), +2d (merchant "refund_issued" with `rf_…` → `refund_promised`; balance unchanged), +3d (statement posts $84.99 with the same reference → exact match → `credit_pending → recovered`, completion evidence = the match id).
6. Ask the agent again: it reports recovery verified by posted credits.

## Demo 2 — Partial recovery (`order_mock_500`)

Same start. The merchant issues $50.00 and it posts: `credit_pending`, final $50.00, outstanding $34.99. The outbox event carries `remaining_minor: 3499`. Ten days after the merchant's claim the credit-wait check moves the case to `issuer_review`; the agent prepares an issuer dispute **for $34.99 only**, flagged as a separate lane needing separate approval, with a fixture deadline alert. The case remains open until $34.99 posts or an explicit outcome is recorded.

## Demo 3 — Provisional reversal (`order_mock_503`)

The merchant claims "refund_issued" for $120.00 but nothing posts. The agent explains the mismatch ("merchant reports the refund as issued … no matching credit has posted; not recovered until it posts"). After the wait window → `issuer_review`; approve the dispute → `issuer_pending`.

- +2d: issuer posts a **provisional** $120.00 → `provisional_credit`; final stays $0.00, outstanding stays $120.00.
- +5d: the provisional credit is reversed → `credit_reversed → issuer_pending`; reversed $120.00, outstanding $120.00.
- +7d: issuer resolves in favour with a new final credit → `final_credit → recovered`; final $120.00, reversed $120.00, outstanding $0.00.

Every step is in the timeline with previous/next state, actor and source event id. The actual output of the last run is in `demo-output.md`.
