---
title: "Inheritance and Dormant-Asset Navigator"
layout: default
nav_order: 1
---

# Inheritance and Dormant-Asset Navigator

A working, local US estate-administration prototype based on [the supplied plan](plan.md). React + TypeScript provides the estate workspace; FastAPI runs the deterministic authorization and claim workflow. All people, institutions, requirements, records, approvals and distributions in the demo are synthetic.

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
- [Verification report](validation.md)
- [Three reproducible demonstrations](demo.md)
- [Inheritance and Dormant-Asset Navigator — Development Plan](plan.md)
- [Implementation Guide](implementation.md)

## Source code

- [backend](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/backend)
- [frontend](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/frontend)
- [tests](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/tests)
- [fixtures](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/fixtures)
- [scripts](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/scripts)
- [migrations](https://github.com/vaibhavkapur/Personal-Finance-Agents/tree/main/projects/Inheritance-and-Dormant-Asset-Navigator/migrations)

## Verification

The imported backend regression suite passes. See [Testing](testing.md) for commands, publication checks, and the scope of previously recorded results.
