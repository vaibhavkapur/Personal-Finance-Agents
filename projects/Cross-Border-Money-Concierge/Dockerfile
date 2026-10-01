FROM node:20-alpine AS web
WORKDIR /src/frontend
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend/ backend/
COPY scripts/ scripts/
COPY fixtures/ fixtures/
COPY --from=web /src/frontend/dist frontend/dist
EXPOSE 8088
CMD ["python", "-m", "uvicorn", "backend.app.api.main:app", "--host", "0.0.0.0", "--port", "8088"]
