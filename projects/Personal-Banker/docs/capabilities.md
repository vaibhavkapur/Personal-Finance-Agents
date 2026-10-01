---
title: "Provider capability matrix"
layout: default
nav_order: 22
---



# Provider capability matrix

Capabilities are labeled **mock**, **sandbox** or **live**. This prototype implements mock execution and a sandbox *shape* for account-data reads. Nothing here is a live bank, aggregator or government integration.

Live values in the table are the capabilities a production adapter would have to advertise honestly; they are not implemented.

| Capability | Harbor / Northwind / Meridian mock | Account-data sandbox adapter | Live (not implemented) |
| --- | --- | --- | --- |
| Environment tag on every result | `mock` | `sandbox` | `production` |
| Read available / current snapshot | yes | only if credentials configured; not implemented | provider-dependent |
| Read product offers | yes (fixture versions, expiry, eligibility) | no | provider-dependent |
| Submit renewal or same-owner transfer | yes | no | requires bank/platform integration + customer auth |
| Lookup write by original `request_ref` | yes | no → uncertain writes go to `manual_review` | required for any write adapter |
| Cancel after bank acceptance | advertised; not exposed to the customer in this MVP | no | separate provider request |
| Signed callbacks | HMAC-SHA256 mock webhooks | no | provider scheme |
| Controllable clock / failure injection | yes | n/a | n/a |
| FAPI | n/a (mock) | n/a | adopt only if the chosen provider uses it |
| AP2 | not required for this account-management journey | — | — |
| A2A bank agents | not enabled | — | later demo; map external task IDs to case IDs |

Source of truth at runtime: `GET /v1/operator/capabilities` (`backend/app/adapters/registry.py`).

**Consequence for this MVP:** all three demo journeys execute against the mock ledger. The sandbox adapter exists so the application never claims a capability it does not have.
