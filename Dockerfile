FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml .
RUN uv pip install --system --no-cache .

ENV PYTHONPATH=/app

COPY alembic.ini .
COPY migrations/ migrations/
COPY app/ app/

EXPOSE 8000

# Migrate before serving: on failure the container exits rather than serving a stale schema.
# Single-container deploy — add a migration lock before scaling to multiple replicas.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*'"]
