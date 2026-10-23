# Deployment

`docker-compose.prod.yml` runs three services from the `prod` stage of `backend/Dockerfile`:

| Service | Role |
|---|---|
| `db` | PostgreSQL 17 with a named volume. Not published to the host. |
| `web` | Applies migrations, creates the cache table, then runs gunicorn (3 workers) on `127.0.0.1:8000`. WhiteNoise serves the admin's static files. |
| `worker` | `run_worker`: scheduled ingestion, Telegram polling and delivery, hourly retention purge. |

All three restart automatically (`restart: unless-stopped`). Only one worker may run per
database; a second one exits. The worker survives database or provider errors: it logs them and
backs off (5 seconds, doubling up to 5 minutes).

## Configure

Create `.env.prod` next to the Compose file. It is git-ignored; never commit it.

```dotenv
DJANGO_SECRET_KEY=<python -c "import secrets; print(secrets.token_urlsafe(64))">
DJANGO_ALLOWED_HOSTS=events.example.org
DJANGO_CSRF_TRUSTED_ORIGINS=https://events.example.org
POSTGRES_PASSWORD=<a long random password>

# Optional
TELEGRAM_BOT_TOKEN=
TELEGRAM_SENDING_ENABLED=false
INGESTION_SCHEDULING_ENABLED=false
DATA_RETENTION_DAYS=30
```

`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` and `POSTGRES_PASSWORD` are required: Compose refuses
to start without them, and Django refuses to start with a placeholder or short secret key.

## Run

```bash
docker compose -f docker-compose.prod.yml --env-file .env.prod up -d --build
docker compose -f docker-compose.prod.yml --env-file .env.prod exec web python manage.py createsuperuser
```

## TLS and the reverse proxy

`web` listens on `127.0.0.1` only. Put a TLS-terminating reverse proxy (Caddy, nginx, Traefik)
in front of it and forward `X-Forwarded-Proto` and `X-Forwarded-For`. The defaults assume this:

- `DJANGO_BEHIND_TLS_PROXY=true` trusts `X-Forwarded-Proto`, so HTTPS redirects and secure cookies
  work. Plain-HTTP requests are redirected to HTTPS.
- `API_NUM_PROXIES=1` makes API throttling use the client IP from `X-Forwarded-For`. Set it to the
  number of proxies in front of Event Bank. If it is wrong, every client shares one throttle
  bucket (too low) or clients can spoof their IP (too high).

HSTS starts at one hour (`DJANGO_SECURE_HSTS_SECONDS=3600`). Raise it once HTTPS is confirmed.

## Throttling and cache

Rate limits are stored in a PostgreSQL cache table (`event_bank_cache`, created by
`createcachetable` on startup), so all gunicorn workers and the worker share one count. No Redis
is needed.

## Data retention

The worker runs `purge_expired_data` hourly; it can also be run by hand. After
`DATA_RETENTION_DAYS` (default 30) it deletes:

- finished deliveries (message bodies and their link between a chat and events);
- `/add` attempt records;
- subscribers with no follows who stopped, or never opted in, and have had no activity since.

Pending deliveries and active subscribers are never purged. Choose a retention period that
matches the privacy notice you publish for your installation (e.g. under Ghana's Data
Protection Act, 2012).

## Backups and updates

Back up the `pgdata` volume, e.g. `docker compose -f docker-compose.prod.yml exec db pg_dump -U
event_bank event_bank > backup.sql`. To update, pull the new code and run the same `up -d --build`
command; `web` applies migrations on start.
