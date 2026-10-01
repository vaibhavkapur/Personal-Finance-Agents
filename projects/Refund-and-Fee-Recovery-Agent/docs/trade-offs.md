---
title: "Trade-off note"
layout: default
nav_order: 24
---


# Trade-off note

**Conservative matching over recall.** A same-merchant credit is only a candidate when its amount equals the outstanding target, the promise, or an amount the merchant has claimed. A $50 credit against an $84.99 promise with no merchant claim is *rejected* (`amount_does_not_match_promise_or_claim`), not offered as a partial match. This avoids stealing another order's refund into this case at the cost of asking the merchant about money that may already have arrived. Eval case `ev_012` documents the behaviour.

**Single unambiguous candidate is auto-confirmed.** One same-merchant credit for exactly the expected amount inside the window closes the case as `already_refunded` without asking. Two or more plausible credits always ask. Confidence is otherwise only a ranking.

**Reminders ride on the original approval scope.** The review screen states the approval covers up to two reminders to the same recipient with the same attachments; the executor checks recipient and attachment hashes before each reminder. This keeps the "bounded reminders" flow unattended without letting the text or destination drift. Any other outbound action needs a fresh approval.

**Uncertain writes: retry the same reference once, then hold.** After a timeout or malformed response we look the action up by `request_ref` (twice, with backoff). If the provider authoritatively has no record we resend with the *same* `request_ref` once. Anything still uncertain, or any adapter without `find_action`, goes to `manual_review`. Availability is traded for never creating a duplicate case.

**Merchant lane first, issuer lane gated.** Disputes are only drafted after the merchant channel declined, claimed a refund that did not post within 10 days, or stayed silent past two reminders. Deadlines come from a versioned fixture config and are surfaced as alerts even while merchant follow-up is in progress. A real deployment needs issuer-specific rules and legal review; the fixture values are placeholders.

**Overlap handling holds rather than corrects.** When final credits cover the target while an issuer provisional credit is still open, or when merchant and issuer final credits exceed the target, the case goes to `manual_review` and outbound approvals are revoked. The correction itself (returning one credit) belongs to the issuer process with customer approval and is out of scope.

**Store credit closes as `unresolved` with a note.** The plan's state list has no "closed with store credit" state; accepting store credit records `closed_with_store_credit:<amount>` on an `unresolved` case rather than inventing a success state. Final recovered stays $0.

**SQLite instead of PostgreSQL.** The schema is portable SQL and the repositories are thin, but the prototype runs on SQLite so it works with zero setup. Concurrency semantics (single writer) are adequate for one API process and one worker; move to PostgreSQL before running multiple workers.

**Deterministic planner instead of an LLM.** No model credentials exist in this repository, so the tool loop, budget, guardrail and evaluation harness run against a rules-based planner. This means the eval report measures the harness and workflow, not model behaviour; `OpenAICompatibleModel` is the swap-in point and is untested here.

**Frontend unverified.** Node was not available in the build environment, so the React/TypeScript views are written against the API contract but were not compiled or rendered.
