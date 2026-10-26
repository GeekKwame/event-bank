FROM ghcr.io/astral-sh/uv:0.13.5 AS uv

FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/opt/venv/bin:$PATH"

COPY --from=uv /uv /usr/local/bin/uv

# Matching the host UID keeps files written to the bind mount (e.g. migrations) owned by you.
ARG APP_UID=1000
ARG APP_GID=1000
RUN groupadd --gid "$APP_GID" app \
    && useradd --uid "$APP_UID" --gid app --home-dir /app --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./

# Development: dev tools included, code bind-mounted, Django's runserver.
FROM base AS dev
RUN uv sync --frozen --no-install-project
COPY --chown=app:app . .
USER app
EXPOSE 8000
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# Production: no dev tools, static files collected at build time, served by gunicorn + WhiteNoise.
FROM base AS prod
RUN uv sync --frozen --no-install-project --no-dev
COPY --chown=app:app . .
RUN DJANGO_DEBUG=true DJANGO_SECRET_KEY=collectstatic-only-not-used-at-runtime \
    python manage.py collectstatic --noinput
USER app
EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", \
     "--access-logfile", "-", "--forwarded-allow-ips", "*"]
