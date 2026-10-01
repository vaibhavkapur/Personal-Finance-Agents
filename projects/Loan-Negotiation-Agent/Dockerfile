FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1

COPY requirements.txt requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]==3.2.3"

COPY backend ./backend
COPY frontend ./frontend
COPY fixtures ./fixtures
COPY scripts ./scripts
COPY migrations ./migrations

EXPOSE 8000
CMD ["python", "scripts/serve.py", "--host", "0.0.0.0", "--port", "8000", "--no-worker"]
