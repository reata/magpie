# magpie

API Server for reata.github.io

## Requirements

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)

## Development

```bash
# Install dependencies
uv sync --locked

# Activate pre-commit hooks (ruff check + ruff format on commit)
uv run pre-commit install

# Run tests
uv run pytest

# Start development server (with hot reload)
uv run uvicorn magpie.main:app --reload --port 8081

# Or use the dev script
uv run dev

# Run lint + format manually (same as pre-commit / CI)
uv run pre-commit run --all-files
```

## Docker

```bash
docker build -t magpie .
docker run --rm -e PORT=8080 -p 8080:8080 magpie
```
