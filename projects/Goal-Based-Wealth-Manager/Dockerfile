FROM node:20-alpine AS frontend
WORKDIR /build/frontend
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
COPY --from=frontend /build/frontend/dist frontend/dist
ENV PYTHONDONTWRITEBYTECODE=1 BIND_HOST=0.0.0.0
EXPOSE 8017
CMD ["python", "scripts/run.py"]
