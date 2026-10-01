---
title: "Mock providers"
layout: default
nav_order: 21
---


# Mock providers

All three adapters support stable-reference initiation lookup, bank-account payouts in INR, exact document-packet sharing, cancellation requests, and operation lookup for follow-up writes. Each result identifies `environment=mock`; no sandbox or production adapter is enabled.

- **SwiftSend:** $5 fee, 83.20 INR/USD fixture rate, 1–4 hour estimated delivery, no modeled recipient deduction. Default: normal delivery. A $500 total budget converts $495 and credits INR 41,184.00.
- **BridgeWay:** $2.99 fee, 83.65 INR/USD fixture rate, 4–12 hour estimate, no modeled deduction. Default: requests a purpose-supporting document. A $500 total budget quotes INR 41,574.89.
- **Lotus Remit:** $1.99 fee, 83.85 INR/USD fixture rate, 12–24 hour estimate, INR 150 known payout deduction already included. Default: accepts initiation then times out. A $500 total budget quotes INR 41,608.14; lookup recovers the original transfer.

No provider always wins: a six-hour deadline admits SwiftSend only, and amount-dependent fee/deduction effects change the value ranking. None guarantees the deadline. Provider amount limits are $10–$10,000 total sender debit.

The rate already includes the simulated FX spread; it is not deducted again. Forward conversion rounds once to target minor units using half-up rounding. Inverse conversion rounds required source minor units upward, then applies the same forward calculation so the net recipient target is met. Original amount basis, rate, sender principal, fee, and deduction remain in the original quote payload.

Scenarios can be overridden before approval. `short_payment` creates a final recipient credit INR 100 below the quote and opens reconciliation review. `delay` withholds delivery and escalates after six hours beyond the estimate. `rejection` rejects initiation. `cancel_denied` leaves the original transfer active. `malformed` models an accepted write with an unusable response and follows the same reference-recovery path as a timeout.
