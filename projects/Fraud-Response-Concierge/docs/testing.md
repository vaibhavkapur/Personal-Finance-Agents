---
title: "Testing"
layout: default
nav_order: 7
---

# Testing

## Publication verification

The backend regression suite was rerun against this imported application on **29 September 2026** using the original implementation's installed Python dependencies. Result: **68 passed, 1 warning in 1.05s**

Application code, fixtures, tests, and dependency lockfiles were copied from the supplied implementation. Documentation was reorganized to match the reference repositories. Docker stacks and browser end-to-end tests were not rerun as part of this migration; earlier results in imported reports are historical.

## Reproduce the checks

After following [Getting Started](getting-started.md):

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m scripts.evaluate
```

Evaluation commands use synthetic fixtures and may refresh checked-in evaluation output. Their results measure the included mock/rules implementation, not live-provider or external-model performance.

## Frontend build

```bash
npm ci --prefix frontend
npm run build --prefix frontend
```

TypeScript checking and the production frontend build both passed during this migration using a separate copy of the original installed dependencies.
