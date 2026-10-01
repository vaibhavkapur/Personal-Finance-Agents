---
title: "Demo script"
layout: default
nav_order: 23
---


# Demo script

One command runs all three scenarios against the in-process API, fixture clock and mock lender network and prints the full transcript (borrower messages, agent replies, review screens, approvals, executor results, state):

```bash
python scripts/demo.py            # all scenarios
python scripts/demo.py --scenario 2
python scripts/demo.py --json     # summary only
```

The same journeys are runnable by hand in the UI (`python scripts/serve.py`, open http://127.0.0.1:8000/). The quick-reply buttons under the conversation issue the exact messages used below. The operator tab exposes the fixture clock, worker, callbacks and fault injection.

Fixtures: existing loan $300,000 at 7.00% with 300 months remaining (P&I $2,120.34, escrow $650). Offers: Northstar 6.50%/300 with $4,500 net incremental costs; Harbor 6.625%/360 with $6,000; Summit 6.75%/300 with $1,500 net (rate-lock section missing). All synthetic.

## Demo 1 — Keep the current loan

1. Create a case with an 18-month horizon and offers A and B.
2. "Is refinancing worth it if I only stay another 18 months?" → the agent records the horizon and asks the two blocking confirmations (balance as of the statement, whether the payment includes escrow). It does not compute anything yet.
3. "Yes, the balance is correct and my payment includes escrow." → `record_borrower_facts`, then `compare_loan_scenarios`. Result at month 18: Northstar costs $2,249.23 *more* than keeping (break-even month 37); Harbor never breaks even. Recommendation: `keep_current`.
4. "OK, I'll keep my current loan." → state `keep_current`; completion evidence is the comparison id.

## Demo 2 — Negotiate comparable terms

1. Create a case with a 48-month horizon and offers A, B, C; confirm facts. Comparison: Northstar is $1,494.25 cheaper than keeping over 48 months (remaining principal $278,106.15 vs $279,553.84); Harbor's lower payment ($199.41/month) reflects a longer term and costs $2,372.91 more; Summit is not ranked (missing rate lock).
2. "Ask my lender whether it can offer better terms." → review screen addressed to the current servicer citing the Northstar terms factually. Approve → the executor sends it → the servicer **refuses**; case returns to `awaiting_decision`.
3. "Ask Harbor to match Northstar." → review screen. Approve → Harbor returns a **versioned counteroffer** (v2: 6.50%/300, but with a $2,000 "rate match pricing adjustment", $8,000 net). The comparison is recalculated: Northstar still wins. Harbor v1 is marked `superseded`.
4. Optional: "Ask Northstar to beat Harbor." → Northstar v2 with a $250 credit. "Ask Summit for better terms." → Summit asks for proof of income; the agent records the request as an open question and does not share anything.

## Demo 3 — Review final changes before mock closing

1. Create a case (48 months, offers A and B), confirm facts, negotiate Harbor to 6.50%/300 as above.
2. "Roll the closing costs into the loan." → financing choice recorded; the comparison shows $0 upfront and $8,000 financed (principal $308,000).
3. "Apply with Harbor." → review screen: destination, product, terms (principal $308,000, P&I $2,079.64), documents, and that it does not authorize a credit inquiry, rate lock, closing or payoff. Approve → mock submission (`app_mock_…`), state `submitted`.
4. Advance the fixture clock 3 days and run the worker → the lender issues final terms with a $600 rate-lock extension fee financed into the loan. `diff_final_terms`: principal $308,000 → $308,600, financed fees $8,000 → $8,600, payment $2,079.64 → $2,083.69 — **material**, so the earlier approval is invalid; a `final_offer` version (v3) is added and the comparison refreshed.
5. "Accept the final terms and close." → a new review screen listing the differences. Approve → the executor requests mock closing → `mock_closed` with `closing_record_id` and a separate `payoff_record_id`. `is_funded` stays `false`; the existing loan is not marked repaid by the closing record alone.

## Variants worth showing

- Northstar path with conditions: apply with Northstar → `conditions_outstanding` (income document) → signed callback delivered (`/v1/ops/mock/deliver-callbacks`, duplicates ignored) → "Send them my pay stub." (`params.document_ids=["doc_paystub_2026_09"]`) → approve → conditions satisfied → final terms with only a prepaid-interest change (non-material) → closing request.
- Fault injection (operator tab): `timeout_after_accept` (application found later by its original reference, no duplicate), `malformed_response` (three lookups then `manual_review`), `declined`, `quote_expired`, `changed_closing_costs`.
- Supplying Summit's rate lock (`POST …/offers/{id}/fields`) makes it rankable without altering the original document.

## Evaluation

```bash
python scripts/eval.py    # 30 labelled cases, agent vs rules-only, writes docs/eval-report.json
```
