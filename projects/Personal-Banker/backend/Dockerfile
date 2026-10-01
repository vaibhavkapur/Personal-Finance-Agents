# Build context is the repository root (see docker-compose.yml).
FROM node:22-alpine AS frontend
WORKDIR /frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS api
# app/config.py resolves the repository root as two levels above app/, so the
# layout inside the image mirrors the repo: /app/app, /fixtures, /frontend/dist.
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pip install --no-cache-dir uv
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv export --frozen --no-dev --no-emit-project -o requirements.txt && pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY backend/scripts ./scripts
COPY fixtures /fixtures
COPY --from=frontend /frontend/dist /frontend/dist
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
