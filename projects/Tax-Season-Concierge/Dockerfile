FROM node:22-alpine AS frontend
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/ ./backend/
COPY migrations/ ./migrations/
COPY rules/ ./rules/
COPY fixtures/ ./fixtures/
COPY scripts/ ./scripts/
COPY --from=frontend /src/frontend/dist ./frontend/dist
ENV PYTHONPATH=/app/backend PYTHONUNBUFFERED=1
RUN useradd --uid 10001 --create-home concierge && mkdir -p /app/.data && chown -R concierge:concierge /app
USER concierge
EXPOSE 8096
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8096"]
