---
title: "Provider capability matrix"
layout: default
nav_order: 22
---



# Provider capability matrix

Label mock, sandbox, and live separately. This prototype ships **mock**. Sandbox is a skeleton. Live is out of scope.

| Capability | Mock (`CLAIMS_ADAPTER=mock`) | A2A mock (`CLAIMS_ADAPTER=a2a`) | Sandbox | Production |
|---|---|---|---|---|
| Environment tag on results | `mock` | `mock` | `sandbox` | `production` (not implemented) |
| `submit_claim` | Yes, in-process simulator | Yes, via A2A `message/send` | Advertised; needs `SANDBOX_BASE_URL` | No |
| `add_evidence` / appeal | Yes | Yes | Advertised; not onboarded | No |
| `get_claim` | Yes | Yes | Advertised | No |
| `find_submission` by request ref | Yes | Yes | **No** — uncertain writes require manual review | No |
| Credentials | None | None | Would stay inside the adapter | — |
| Controllable clock | Fixture clock | Same simulator | N/A | — |
| Decision paths | approved / evidence requested / partial / denied | Same | — | — |
| Fault injection | timeout after/before accept, malformed, declined, delayed callback | Same simulator | — | — |
| Duplicate / out-of-order events | Toggleable | Same | — | — |
| Payout feed | Mock credits (exact, smaller, split, provisional, unrelated, wrong payee) | Same | — | Not a customer checkout (AP2 unused) |
| Uncertain outcome handling | Lookup by request ref, then hold | Same | `uncertain_requires_manual_review=true` | — |

`GET /v1/provider/capabilities` and `/health` return the live adapter advertisement.

Every adapter write records `environment` and `authority` on the result. The sandbox adapter raises `ProviderNotConfigured` until a real sandbox URL is set; it does not pretend to be live.
