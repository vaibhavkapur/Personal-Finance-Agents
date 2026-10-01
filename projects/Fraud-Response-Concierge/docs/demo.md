---
title: "Three demo scripts"
layout: default
nav_order: 23
---


# Three demo scripts

The executable version is `.venv/bin/python -m scripts.demo`. It creates an isolated temporary fixture database and asserts every checkpoint.

## 1. Two-bank containment

Open a new incident and confirm both affected cards. Review and approve Cedar's temporary lock and then Northstar's temporary lock. The worker verifies Cedar protection while Northstar remains explicitly unresolved. The aggregate reads “Partly contained.” Click “Continue with bank,” inspect the trusted simulated destination, and complete the mock authentication. Both cards then have verified protection and separate provider references. No authentication secret is requested.

## 2. Unfamiliar but legitimate

Open `SQ * WESTSIDE COFFEE`. Read the merchant descriptor context and choose “I recognize this.” The exact customer statement is stored, no charge report is sent, and no bank fraud determination is fabricated. The other transaction remains independent.

## 3. Long investigation and recovery

Confirm the Nova Electronics charge as unauthorized, then review and approve its report. In the simulator, issue a provisional credit: the credit is visible and the investigation remains open. Issue a question, return to Transactions, and prepare and approve a factual response. In Recovery, review and approve each lost-card report and replacement. Simulate both deliveries. Record the recurring-payment attestations. Issue the final favorable investigation result. Only then can the case close. Finally, issue a credit reversal: the closed case reopens with its earlier history intact.

## Additional fault exercises

Select “Accept, then time out” before preparing a new action. The action remains unknown until reconciliation finds its original provider reference. The provider-write count does not increase when reconciling. “Delay lock verification” needs the fixture clock advanced by five minutes. “Malformed acknowledgment” holds the action for review; a provider lookup can recover it. “Decline the request” remains visible and does not block the other institution. Never treat a request acknowledgment as verified protection.
