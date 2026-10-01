FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/backend

COPY pyproject.toml README.md ./
COPY backend ./backend
COPY fixtures ./fixtures
COPY scripts ./scripts
COPY migrations ./migrations

RUN pip install --no-cache-dir ".[postgres]"

EXPOSE 8000
CMD ["python", "-m", "app.main"]
