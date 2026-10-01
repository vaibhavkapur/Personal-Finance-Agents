---
title: "Demo script"
layout: default
nav_order: 23
---



# Demo script

Three recorded journeys. All data is synthetic. The fixture clock in `scripts/demo.py` is frozen at **2026-09-14T10:00Z**. Nothing is submitted to a real insurer.

```bash
python scripts/demo.py              # all three
python scripts/demo.py complete
python scripts/demo.py missing-evidence
python scripts/demo.py partial-rejection --export /tmp/case.json
```

## Demo 1 — Complete claim

**Setup.** Jordan Rivera, policy `travel_policy_demo_1` version 2026-09 (loss 10 September 2026). Documents: itinerary, PIR, arrival confirmation, receipts $35 / $45 / $70 / $40 (excluded electronics).

**Steps**

1. Open the case. Policy version is selected from the loss date (2026-09, $120 cap, 12-hour delay), not the January sample.
2. Ask the advocate: *“My bag arrived two days late. Help me claim the essential purchases I made.”*
3. Review the exact packet: destination (mock insurer), documents and hashes, requested vs fixture maximum, excluded $40 disclosed, irreversible “this will file a claim.”
4. Approve. Worker submits once with an idempotent request reference and stores `insurer_mock_…`.
5. Decision: approved at the cap ($120). Citations point at clause offsets in the fixture text.
6. Mock payout feed posts the exact approved amount. Case → `paid` → `closed`. Accepted = paid; outstanding = $0.

**UI path.** Customer → Open a new case → select the complete document set → simulator path `approved` (or leave blank; arrival confirmation is present) → send the advocate message → **Approve exactly this packet** → Operator → **Run worker until idle** → Customer → **Reconcile payout**.

## Demo 2 — Missing evidence

**Setup.** Same receipts, **no** arrival confirmation. Simulator path `evidence_requested` (or omit arrival so the mock asks for it).

**Steps**

1. Case opens with `missing_fields: ["baggage_arrival_confirmation"]`. Drafting a submission is refused until the customer states a delivery time (or attaches the confirmation).
2. Customer answers `baggage_delivered_at` = `2026-09-12T16:30:00Z`.
3. First packet is submitted on the original claim. Insurer requests `baggage_arrival_confirmation`, due 14 days later on the fixture clock, deadline source recorded.
4. Customer attaches `arrival_confirmation_demo`. Advocate resumes the **same** case (same `external_claim_ref`, submission sequence 2).
5. Supplemental packet is approved and sent. Request marked satisfied by that submission.
6. Decision arrives. Optional: post a **smaller** credit then a **remainder** so accepted / paid / outstanding stay distinguishable until the second credit lands.

**Restart.** The worker can be stopped after the first submit. Jobs and the open insurer request persist. Starting the worker again continues the same case; it does not open a second claim.

## Demo 3 — Partial rejection

**Setup.** Include `receipt_demo_3` (mug purchased ~24.5 hours after delay start). The **fixture policy** has no 24-hour purchase cutoff; the **mock adjuster** applies an internal 24-hour guideline (`RC_LATE_PURCHASE`). That is intentional: it creates a supported challenge.

**Steps**

1. Submit the packet. Decision is partial: clothing/toiletries accepted, electronics excluded (supported by policy C7.4), late mug rejected by the adjuster guideline.
2. Explanation shows each rejected line with `policy_view` (`supported_by_policy` vs `contradicted_by_evidence`) and clause citations / fact locators.
3. *“Can we challenge this?”* — appeal draft only for the mug (evidence + policy window C7.3). The electronics exclusion is explained, not appealed.
4. Customer reviews the appeal packet and approves. Worker sends a written-review request on the same claim.
5. Review outcome is applied. If the challenge is accepted, settlement matches the new accepted amount. If the insurer’s reason is supported, the agent explains that and does not generate a more aggressive unsupported appeal.

## Operator checks (any demo)

- Adapter request log (redacted): operation, request ref, outcome, `environment=mock`.
- State transitions with expected case version.
- Tool runs: tool name, latency, redacted input/output refs.
- Mock faults: timeout after accept (lookup by request ref; no duplicate write), declined (no side effect; re-approve with a new ref), malformed, delayed callback (advance the fixture clock).
- Payout modes: exact, smaller, remainder, provisional, unrelated claim, wrong payee.
