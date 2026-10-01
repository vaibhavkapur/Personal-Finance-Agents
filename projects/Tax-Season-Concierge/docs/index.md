---
title: "Tax-Season Concierge"
layout: default
nav_order: 1
---

# Tax-Season Concierge

A runnable 2025 US federal tax-preparation prototype: explicit eligibility, synthetic document intake, duplicate/W-2c reconciliation, deterministic IRS Tax Table calculations, line-level evidence, exact-package approval, durable mock filing, and separate refund matching.

This repository contains the implemented application, tests, synthetic fixtures, and setup files. It runs against mock providers; the original development plan remains in the local project folder and is excluded from this repository.

## Documentation

- [Getting Started](getting-started.md)
- [Architecture](architecture.md)
- [API Reference](api-reference.md)
- [Configuration](configuration.md)
- [Database](database.md)
- [Testing](testing.md)
- [Deployment](deployment.md)
- [Application API](api.md)
- [MCP integration](mcp.md)
- [Demo walkthroughs](demo-script.md)
- [Tax-Season Concierge — Development Plan](plan.md)
- [Verification report](test-report.md)
- [Implementation Guide](implementation.md)

## Source code

- [backend](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/backend)
- [frontend](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/frontend)
- [tests](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/tests)
- [fixtures](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/fixtures)
- [scripts](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/scripts)
- [migrations](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Tax-Season-Concierge/migrations)

## Verification

The imported backend regression suite passes. See [Testing](testing.md) for commands, publication checks, and the scope of previously recorded results.
