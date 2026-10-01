---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/.env.example)

```dotenv
# Defaults are for a loopback-only, synthetic demo. Do not store real customer data.
DATABASE_URL=sqlite:///./concierge.db
ENABLE_DEMO=1
PROVIDER_SHARED_SECRET=local-fixture-webhook-secret
# MOCK_PROVIDER_URL=http://127.0.0.1:8094
# APP_ORIGINS=http://127.0.0.1:8093
# COOKIE_SECURE=1
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/requirements.txt)
- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Fraud-Response-Concierge/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
