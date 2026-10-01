---
title: "Deployment"
layout: default
nav_order: 8
---

# Deployment

## Run the application

Use [Getting Started](getting-started.md) for the native local setup and [Configuration](configuration.md) for the actual runtime settings. These applications use synthetic data and mock providers by default.

The repository also includes [Docker Compose configuration](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/docker-compose.yml) and [the application image definition](https://github.com/vaibhavkapur/Personal-Finance-Agents/blob/main/projects/Loan-Negotiation-Agent/Dockerfile). Review the supplied service configuration before running:

```bash
docker compose up --build
```

This migration verified native tests and did not rerun the container stack.

## Documentation publishing

The documentation is available in this project’s [documentation index](index.md) inside the Personal Finance Agents repository. The standalone GitHub Pages site was retired with its original repository on 1 October 2026. The Jekyll configuration remains available for local preview; Personal Finance Agents does not currently publish a replacement Pages site.

For a local documentation preview, use Ruby 3.1+ and Bundler:

```bash
cd docs
bundle config set --local path vendor/bundle
bundle install
bundle exec jekyll serve --baseurl ""
```

Open `http://127.0.0.1:4000`. Dependency installation and the pinned remote theme require network access.
