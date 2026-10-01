---
title: "Provider capability matrix"
layout: default
nav_order: 22
---


# Provider capability matrix

Every adapter result is stamped with `environment` (`mock` | `sandbox` | `production`) and `authority` (`authoritative` | `estimated` | `simulated`). Capabilities are advertised by the adapter (`GET /v1/lenders` → `adapter_capabilities`), never assumed.

| Capability | Mock network (`adapters/mock_lender.py`) | Sandbox (`adapters/sandbox.py`) | Production |
| --- | --- | --- | --- |
| `request_offer` | no (offers arrive as Loan Estimate documents) | no | not integrated |
| `send_negotiation` | yes — refuse / small credit / match rate + fees / request income proof | no | not integrated |
| `get_negotiation_by_request_ref` | yes | no | — |
| `submit_application` | yes — accept, conditions, decline, timeout-after-accept | no | not integrated |
| `get_application_by_request_ref` | yes (used for reconciliation and polling) | no | — |
| `provide_documents` | yes (satisfies `income_doc_missing`, `employment_verification`) | no | — |
| `request_closing` | yes — returns closing record + separate payoff record | no | — |
| webhooks / callbacks | yes — HMAC-SHA256 signed, deliverable on demand, delayable | no | — |
| controllable clock | yes (`FixtureClock`) | n/a | n/a |
| fault injection | timeout after accept, malformed response, declined, delayed callback, quote expired, changed closing costs | n/a | n/a |
| uncertain outcome requires manual review | no (lookup by reference available) | **yes** (advertised; executor holds for review) | to be determined per provider |
| credentials required | none | would be required (never logged) | required |

## Mock lender behaviours (fixtures/lenders.json)

| Lender id | Name | Negotiation | Application |
| --- | --- | --- | --- |
| `lender_mock_servicer` | Fixture Servicing Co. (current servicer) | refuses to reprice | not offered (declines) |
| `lender_mock_a` | Northstar Mortgage | counteroffer with +$250 lender credit | accepts; requests income document; final terms change only prepaid interest (non-material) |
| `lender_mock_b` | Harbor Home Loans | matches competing rate/term, adds $2,000 pricing adjustment | accepts; final terms add a $600 rate-lock extension fee (material) |
| `lender_mock_c` | Summit Lending | requests income evidence before repricing; with evidence: +$500 credit and lock terms | accepts; requests income and employment verification |

## A2A boundary

Each mock lender is also reachable as an A2A agent (`/a2a/lenders/{id}`, protocol `0.3`, payload schema `loan-offer/v1`). The `A2ALenderAdapter` pins both and refuses agents that advertise anything else. Task ids map to internal client request references. Lender APIs (the adapter above) remain the execution interface for applications, documents and closing.

## What is not simulated

Credit pulls, rate locks, funding, disbursement, real payoff, TRID timing rules, compliant APR computation, adjustable-rate or government-backed products. Success rates observed against the simulator are not customer or market results.
