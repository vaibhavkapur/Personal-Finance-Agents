---
title: "Demo walkthroughs"
layout: default
nav_order: 23
---


# Demo walkthroughs

## Two jobs, one return

Choose the two-jobs case. Confirm the sample profile, import three documents and inspect the W-2 fields. Federal withholding is distinct from Social Security and Medicare withholding. Attest completeness, reconcile and prepare the draft. Open line 1a to see both wage sources. Open line 16 to see the official table row. Review the exact package and approve mock submission. Advance one day: acceptance with refund pending. Advance a second day: refund notice, still pending. Advance a third day: account credit matches and the refund is verified.

Command: `.venv/bin/python scripts/demo.py --scenario two_jobs`.

## Corrected form

Prepare the ordinary draft, then return to Documents and add the sample correction. The new W-2c changes only Northstar withholding. The old package becomes invalid, original wage lineage remains, and completeness must be reconfirmed. Reconcile, prepare the new draft, review the $1,362 potential refund and approve the new exact package. After acceptance, importing another document routes to amendment review and preserves the submitted result.

Command: `.venv/bin/python scripts/demo.py --scenario corrected_form`.

## No refund

Create a balance-due sample case. Complete the same document and review flow. The result is $2,339 due. Approval only submits to the mock provider. After acceptance, the financial panel shows balance-due follow-up. No bank debit, tax payment or refund is fabricated.

Command: `.venv/bin/python scripts/demo.py --scenario balance_due`.

## Failure recovery

The timeout scenario stores an accepted provider action but drops the response. The worker holds the outcome as unknown and queries the original request reference. It recovers one submission without a second write. The rejection scenario requires explicit identity review and a new package/approval. The delayed scenario accepts on day 3, issues a notice on day 5 and supplies account evidence on day 7.

Inspect Operator view and Activity for concise decisions, case versions, stable references and authority evidence. The tool log contains no hidden model reasoning or provider credentials.
