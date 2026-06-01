# magpie

API Sever for reata.github.io

## Requirements

- Python 3.10+
- [uv](https://docs.astral.sh/uv/)

## Development

```bash
# Install dependencies
uv sync

# Start development server (with hot reload)
uv run uvicorn magpie.main:app --reload --port 8081

# Or use the dev script
uv run dev

# Run linter
uv run ruff check magpie/

# Auto-fix lint issues
uv run ruff check magpie/ --fix
```

## Docker

```bash
docker build -t magpie .
docker run --rm -e PORT=8080 -p 8080:8080 magpie
```
