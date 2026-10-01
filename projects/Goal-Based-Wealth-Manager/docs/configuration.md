---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/.env.example)

```dotenv
# Local synthetic environment only; no live account credentials are used.
WEALTH_DB=var/wealth.db
CUSTODIAN_DB=var/custodian.db
MOCK_WEBHOOK_SECRET=local-demo-webhook-secret
BIND_HOST=127.0.0.1
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/requirements.txt)
- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Goal-Based-Wealth-Manager/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
