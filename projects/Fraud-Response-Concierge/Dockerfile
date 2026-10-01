FROM node:20-bookworm-slim AS ui
WORKDIR /build
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build
FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
COPY scripts ./scripts
COPY migrations ./migrations
COPY --from=ui /build/dist ./frontend/dist
EXPOSE 8093
CMD ["python","-m","uvicorn","backend.app.api.main:app","--host","0.0.0.0","--port","8093","--no-access-log"]
