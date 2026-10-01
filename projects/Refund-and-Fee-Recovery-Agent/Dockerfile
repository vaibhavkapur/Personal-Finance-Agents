FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml ./
COPY backend ./backend
COPY fixtures ./fixtures
COPY migrations ./migrations
COPY scripts ./scripts
RUN pip install --no-cache-dir -e .
EXPOSE 8000
CMD ["uvicorn", "backend.app.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
