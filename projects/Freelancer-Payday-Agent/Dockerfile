FROM node:22-alpine AS frontend
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/index.html frontend/tsconfig.json frontend/vite.config.ts ./
COPY frontend/src ./src
RUN npm run build

FROM python:3.12-slim
WORKDIR /app
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY backend ./backend
COPY migrations ./migrations
COPY fixtures ./fixtures
COPY --from=frontend /build/frontend/dist ./frontend/dist
RUN useradd --create-home payday && mkdir /data && chown payday:payday /data
USER payday
ENV PAYDAY_DATA_DIR=/data PYTHONUNBUFFERED=1
EXPOSE 8012
CMD ["python", "-m", "uvicorn", "backend.app.main:app", "--host", "0.0.0.0", "--port", "8012"]
