---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

## Environment reference

[Environment example](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/.env.example)

```dotenv
PAYDAY_DATA_DIR=data
PAYDAY_DEMO_LOGIN=1
PAYDAY_ORIGINS=http://127.0.0.1:8012,http://localhost:8012,http://127.0.0.1:5182,http://localhost:5182
# Optional overrides; absent secrets are generated locally with owner-only permissions.
# PAYDAY_DEMO_TOKEN=replace-with-a-long-random-local-token
# PAYDAY_WEBHOOK_SECRET=replace-with-a-different-long-random-key
```

Follow the comments in the template. Export overrides in the launcher shell unless the application explicitly loads a dotenv file; copying a file alone does not configure every launcher.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/requirements.txt)
- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Freelancer-Payday-Agent/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
