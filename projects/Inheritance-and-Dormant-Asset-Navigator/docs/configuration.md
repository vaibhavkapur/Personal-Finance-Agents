---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/.env.example)

```dotenv
# Local synthetic prototype only. This file is a configuration reference;
# export values in your shell before starting uvicorn or the worker.
DEMO_PASSWORD=everkeep-demo
EMBEDDED_WORKER=1
ALLOWED_ORIGINS=http://127.0.0.1:8731,http://localhost:8731,http://127.0.0.1:5187
# DATABASE_URL=postgresql+psycopg://estate:password@localhost/estate
# ESTATE_ENCRYPTION_KEY=<Fernet key; use the same key in API, worker, provider>
# SESSION_SECRET=<random server-only value>
# PROVIDER_WEBHOOK_SECRET=<random callback signature key>
# MOCK_PROVIDER_URL=http://127.0.0.1:8732
# MOCK_PROVIDER_TOKEN=<shared local provider token>
# SECURE_COOKIES=1  # Requires HTTPS; off for loopback development.
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/requirements.txt)
- [requirements.lock.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/requirements.lock.txt)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Inheritance-and-Dormant-Asset-Navigator/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
