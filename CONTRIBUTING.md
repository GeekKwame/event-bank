# Contributing

Thanks for helping build Event Bank. The most common contribution is a new source connector; see
[docs/adding-a-connector.md](docs/adding-a-connector.md).

## Development setup

```bash
cp .env.example .env
docker compose up --build
docker compose exec backend pytest
```

Everything runs in Docker. To work outside Docker, install [uv](https://docs.astral.sh/uv/) and
run `uv sync` in `backend/`. You will need a PostgreSQL database configured through the
`POSTGRES_*` variables.

## Before opening a pull request

```bash
docker compose exec backend ruff check .
docker compose exec backend ruff format --check .
docker compose exec backend python manage.py makemigrations --check --dry-run
docker compose exec backend pytest
```

CI runs the same checks, plus a dependency audit and a secret scan.

## Guidelines

- Keep `connectors/` and `pipeline/` free of Django imports.
- Keep provider-specific code (mNotify, Telegram, ...) in `apps/notifications/providers/`.
- Tests must not use the network: use `httpx.MockTransport` and a fake DNS resolver.
- Every new API view must declare `permission_classes` explicitly.
- Never commit secrets, real phone numbers or personal data, including in fixtures.
- Add or update documentation when behaviour changes.
- Report security issues privately (see [SECURITY.md](SECURITY.md)).

## License of contributions

Event Bank currently has no license, and no terms for contributions have been set. Contact the
maintainer before contributing.
