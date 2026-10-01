FROM node:22-bookworm-slim AS frontend
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements*.txt ./
RUN pip install --no-cache-dir -r requirements.lock.txt
COPY backend/ ./backend/
COPY fixtures/ ./fixtures/
COPY --from=frontend /build/dist ./frontend/dist/
RUN mkdir -p /app/data && chmod 700 /app/data
EXPOSE 8731
CMD ["uvicorn", "backend.app.api.main:app", "--host", "0.0.0.0", "--port", "8731"]
