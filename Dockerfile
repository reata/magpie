FROM python:3.10-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Install dependencies (layer cached)
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Install project
COPY magpie ./magpie
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Run as non-root user
RUN adduser --quiet magpie
USER magpie

CMD uvicorn magpie.main:app --host 0.0.0.0 --port $PORT
