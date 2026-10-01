---
title: "MCP integration"
layout: default
nav_order: 22
---


# MCP integration

The server implements newline-delimited JSON-RPC over stdio, pinned to MCP protocol **2025-06-18**. It supports initialize, ping, tools/list and tools/call; notifications are accepted without a response. There are five typed tools:

- `check_supported_profile`
- `reconcile_tax_documents`
- `calculate_federal_return`
- `prepare_return_package`
- `get_filing_and_refund_status`

Start it as a trusted local operator:

```sh
PYTHONPATH=backend TAX_MCP_TENANT=demo_your_existing_tenant DATABASE_URL=.data/tax.db .venv/bin/python -m app.agent.mcp
```

Use the tenant ID issued to your demo session. This is a trusted local stdio boundary, not a multi-user remote authentication endpoint. The process has direct database access; do not accept the tenant environment setting from untrusted documents or a remote client.

Tool arguments include `case_id`; mutating tools require `expected_case_version`. Calculation additionally requires `rule_pack_id` and `facts_hash`. All results include source, retrieval time, authority and environment. Approval challenges are removed from model-visible mutation results. No approve, submit, refund redirection or permission-changing tool is exposed. The customer attests completeness through the API; the tool cannot invent that attestation.

The shipped concierge (`/assist`) has at most eight tool calls per run and stops at missing information or customer review. `StructuredToolCaller` is the optional external-model boundary; no hosted model is connected or measured.

Protocol references: https://modelcontextprotocol.io/specification/2025-06-18/basic/transports and https://modelcontextprotocol.io/specification/2025-06-18/server/tools.
