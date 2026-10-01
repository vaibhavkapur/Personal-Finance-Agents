---
title: "Demo walkthrough"
layout: default
nav_order: 22
---


# Demo walkthrough

Run `./scripts/dev.sh`, then open http://127.0.0.1:8012. The fixture date begins on 1 October 2026. The user is Alex Morgan, a sole-proprietor designer with a business account ending 4821 and personal account ending 0916.

## 1. Supported payday

Overview initially shows $10,000 available, $6,000 protected, and a separate $5,000 overdue invoice. The supported payday is $3,000. Click **Review your payday**, verify the exact amount and destination, check the approval statement, then approve. The debit and credit are matched before the UI says reconciled. Business cash becomes $7,000; the three protected buckets remain $2,000 each and flexible cash becomes $1,000. Operations shows both bank entry references and conserving journal lines.

## 2. Client pays late

Use a fresh workspace, or return the first payout through Operations. Click **Client stays late; business spends $2,500**. The planner lowers the next supported payout to $1,500; it does not use the overdue invoice. Then click **Orbit pays its $5,000 invoice** to import an actual posted payment and revise the plan. Repeating that import must not add cash or a tax allocation twice.

## 3. Timeout and return

Before reviewing a new payout, select **Accepted, then connection times out** in Operations. Approve the exact action in Overview. The payout enters `outcome_unknown` with its amount still reserved. Click **Post pending bank transfers**. The existing transfer is found by its original request reference and reconciled; no second transfer is created. Now click **Return the latest posted payout**. The original history remains, new return evidence is recorded, the cash is restored, and the case moves to recovery review. Calculate a fresh plan and review again before any replacement payout.

## Other controls

- Import an ambiguous receipt; planning is blocked until Activity confirms its category.
- Edit a reserve policy; inspect old and new settings before the approval step. Unsubmitted payout reviews become stale.
- Advance the fixture clock an hour while a review is open; approval expires and no transfer executes.
- Ask the assistant to explain late payments or classify receipts; Operations shows its typed tool calls.
- Try a declined or malformed response. Declines release reservations only after confirmed non-execution; uncertain outcomes keep them.

`python -m scripts.demo` executes the three core scenarios without any server, using fresh temporary state each time. `python -m scripts.evaluate` executes the 30 labeled assistant scenarios and saves their actual checks to `evaluation-report.json`.
