---
title: "Configuration"
layout: default
nav_order: 5
---

# Configuration

Configuration below applies to the implemented local prototype. Provider names, default passwords, and example callback secrets in the checked-in templates are synthetic demo values.

- `TRADING_ENVIRONMENT`: must be `mock` (default).
- `DESK_DATA_DIR`: local runtime directory, default `data`; application and broker SQLite files are separate.
- `DESK_DEMO_PASSWORD`: local fixture login, default `paper-demo`.
- `DESK_EMBEDDED_WORKER`: defaults to `1`; set to `0` if running the separate worker.
- `DESK_SECURE_COOKIE`: `1` enables the secure-cookie flag, which requires HTTPS.
- `DESK_WEBHOOK_SECRET`: shared secret for verifying broker callbacks when using that endpoint.

Export overrides in the shell before starting the app. No dotenv loader or live brokerage credentials are required for the default mock path.

## Dependency versions

- [requirements.txt](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/requirements.txt)
- [requirements.lock](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/requirements.lock)
- [pyproject.toml](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/pyproject.toml)
- [frontend/package.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/frontend/package.json)
- [frontend/package-lock.json](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Personal-Trading-Desk/frontend/package-lock.json)

## Documentation site

`_config.yml` selects the dark Just the Docs theme and this repository's GitHub Pages path. See [Deployment](deployment.md) for publishing and preview commands.
