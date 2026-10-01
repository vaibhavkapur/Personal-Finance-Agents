---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/.env.example)

```dotenv
# Copy to backend/.env. All settings are optional; defaults run the mock environment on SQLite.
PB_DATABASE_URL=sqlite:///./personal_banker.db
# PB_DATABASE_URL=postgresql+psycopg://banker:banker@localhost:5432/personal_banker
PB_ENVIRONMENT=mock
PB_CLOCK_START=2026-09-26T09:00:00Z
PB_WEBHOOK_SECRET=change-me-for-any-shared-deployment
PB_AGENT_TOOL_BUDGET=12

# Optional OpenAI-compatible model for the agent policy. Unset = deterministic rules policy.
# PB_LLM_BASE_URL=https://api.openai.com/v1
# PB_LLM_API_KEY=
# PB_LLM_MODEL=gpt-4o-mini

# Optional account-data sandbox credentials (read-only capability; not required).
# PB_PLAID_CLIENT_ID=
# PB_PLAID_SECRET=
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Runtime settings in source

- [backend/app/config.py](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/backend/app/config.py)

## Dependency versions

- [backend/pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/backend/pyproject.toml)
- [backend/uv.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/backend/uv.lock)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Banker/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
