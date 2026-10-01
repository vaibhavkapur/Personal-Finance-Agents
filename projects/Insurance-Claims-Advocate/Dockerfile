FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/srv/backend
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt "psycopg[binary]>=3.1,<4"
COPY backend ./backend
COPY frontend ./frontend
COPY fixtures ./fixtures
COPY scripts ./scripts
COPY migrations ./migrations
EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--app-dir", "backend", "--host", "0.0.0.0", "--port", "8000"]
