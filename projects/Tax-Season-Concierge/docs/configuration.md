---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/.env.example)

```dotenv
# Local synthetic-data prototype. Export these values in your shell.
# In-process simulator and SQLite require no external credentials.
# DATABASE_URL=postgresql://concierge:synthetic_demo_only@localhost:5496/tax_concierge
RUN_WORKER=1
COOKIE_SECURE=0
# Set a stable random value to test signed callbacks across restarts.
# WEBHOOK_SECRET=
# Optional separate mock provider:
# MOCK_PROVIDER_URL=http://127.0.0.1:8097
# MOCK_PROVIDER_TOKEN=
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/requirements.txt)
- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Tax-Season-Concierge/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
