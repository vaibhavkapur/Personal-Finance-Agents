---
title: "Testing"
layout: default
nav_order: 7
---

# Testing

## Publication verification

The backend regression suite was rerun against this imported application on **29 September 2026** using the original implementation's installed Python dependencies. Result: **Backend regression suite passed (exit code 0)**

Application code, fixtures, tests, and dependency lockfiles were copied from the supplied implementation. Documentation was reorganized to match the reference repositories. Docker stacks and browser end-to-end tests were not rerun as part of this migration; earlier results in imported reports are historical.

## Reproduce the checks

After following [Getting Started](getting-started.md):

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/run_evals.py
```

Evaluation commands use synthetic fixtures and may refresh checked-in evaluation output. Their results measure the included mock/rules implementation, not live-provider or external-model performance.

## Frontend

The API serves the included static frontend directly; this project has no npm build step.
