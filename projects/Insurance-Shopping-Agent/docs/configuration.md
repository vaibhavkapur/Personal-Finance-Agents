---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/.env.example)

```dotenv
# Copy to .env for local runs. Everything defaults to a credential-free mock environment.
DATABASE_URL=sqlite:///./data/app.db
APP_ENVIRONMENT=mock            # mock | sandbox | production (only mock is implemented)
ADAPTER_MODE=direct             # direct (in-process mock insurers) | a2a (talk to scripts/run_insurer.py agents)
A2A_URL_NORTHWIND=http://localhost:9001
A2A_URL_HARBORLINE=http://localhost:9002
A2A_URL_CEDAR=http://localhost:9003
FIXTURE_CLOCK=true
FIXTURE_CLOCK_START=2026-10-01T09:00:00+00:00
FIXTURE_CLOCK_FROZEN=false      # true only for deterministic tests
EMBEDDED_WORKER=true            # run the job worker inside the API process (local convenience)
TOOL_CALL_BUDGET=8
APPROVAL_TTL_MINUTES=30
WEBHOOK_SIGNING_SECRET=local-dev-outbound-secret
# OUTBOUND_WEBHOOK_URL=http://localhost:9999/events

# Optional LLM. Without a key the deterministic scripted provider is used.
LLM_PROVIDER=scripted           # scripted | openai_compatible
# LLM_API_KEY=
# LLM_BASE_URL=https://api.openai.com/v1
# LLM_MODEL=gpt-4o-mini

# MCP server scope
# MCP_CUSTOMER_TOKEN=tok_cus_demo_1
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Runtime settings in source

- [backend/app/config.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/backend/app/config.py)

## Dependency versions

- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Insurance-Shopping-Agent/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
