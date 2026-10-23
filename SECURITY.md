# Security

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's private vulnerability
reporting on this repository ("Security" tab → "Report a vulnerability"). We aim to acknowledge
reports within 7 days.

## Supported versions

Event Bank has no releases yet. Security fixes are applied to `main`.

## Threat model and controls

### Server-side request forgery (SSRF) from source URLs

Source URLs come from users and are fetched by the server. Controls in `backend/connectors/http.py`:

- Only `https://` URLs, on port 443, with no credentials and no IP-literal hosts.
- The host must be in the matched connector's `allowed_hosts`. Sources also need administrator
  approval before they are fetched.
- Every DNS answer must be a public address. Loopback, private, link-local (including cloud metadata
  such as `169.254.169.254`), carrier-grade NAT, multicast, reserved, documentation, unique-local IPv6,
  and IPv4 embedded in IPv6 (mapped, 6to4, Teredo, NAT64) are rejected. One unsafe answer rejects the host.
- The connection goes to the vetted IP, with the original `Host` header and TLS SNI. Certificates
  are verified against the hostname. DNS is resolved once per hop, so rebinding cannot change the target.
- Redirects are followed manually (max 3), and each hop is fully re-validated.
- Proxy environment variables are ignored, keep-alive connections are not reused, and no cookies are sent.
- Responses are limited to 2 MiB, compressed responses are refused, and requests have connect and read
  timeouts plus a 20 second total deadline.

### Secrets

- Secrets come from environment variables only. `.env` is git-ignored, and CI runs a gitleaks scan.
- Connector credentials (e.g. Luma API keys) are read from the environment at run time. A source's
  config can only name an allowed variable (`LUMA_API_KEY` / `LUMA_API_KEY_*`), never hold the secret.
  Luma calendar keys grant full access to their calendar. They are sent only in a header to
  `public-api.luma.com`, never in URLs, and never forwarded on cross-host redirects.
- Log redaction also covers the values of environment variables whose names contain `KEY`, `SECRET`,
  `TOKEN` or `PASSWORD` (values of 8 or more characters).
- With `DEBUG` off, the app refuses to start with a placeholder or short `DJANGO_SECRET_KEY`.
- All log output passes through a formatter that redacts the configured secrets and secret-looking
  query parameters (`key=`, `token=`, ...), including in tracebacks. The `httpx` and `httpcore`
  loggers are limited to warnings because mNotify puts its API key in the URL.
- Provider and ingestion errors store only safe messages. Unexpected exceptions are recorded by type name only.

### Third-party data

- Luma is accessed only through publicly supported interfaces (its free iCal subscription feeds and
  its official API), as its Terms of Use require. No luma.com pages are scraped, and the test
  fixtures are synthetic.
- The iCal connector rejects personal feeds (`entity=user`), which carry a private token, and
  never reads organiser or attendee data or stores link-type locations such as meeting URLs.
- Only public events managed by the registered calendar are stored. Events managed by other hosts
  are excluded. Guest-only addresses are reduced to the city. Meeting links and submitter details
  (including emails) are never stored, and personal calendar names are not stored as organisers.
- Cover images are stored as URLs only. Luma's terms forbid downloading images as stand-alone files.

### Notification abuse

- Real SMS sending is off unless `SMS_SENDING_ENABLED=true`; there is no SMS subscriber workflow yet.
- Real Telegram sending is off unless `TELEGRAM_SENDING_ENABLED=true`. Only private chats are
  supported. `/follow` records consent; `/stop` removes follows and cancels queued event alerts.
  Incoming commands are obtained from Telegram's authenticated API; chat IDs cannot be supplied
  through a public subscription endpoint. The bot token is redacted even when embedded in a URL.
- One worker per database paces Telegram messages at 1.1 seconds each with paid broadcasts disabled.
  A run that would send someone more than five alerts sends one digest instead, and command
  replies (including `/stop` confirmations) are sent before queued alerts.
- `/add` is limited per chat to five submissions and ten attempts a day; failed attempts count, so
  repeated invalid URLs can't trigger unlimited DNS lookups. Duplicate submissions only reveal
  sources that are already approved.
- A malformed or unexpected Telegram message is answered with a generic error and skipped; it
  can't block later messages or stop the worker.
- Personal data is minimised and expires: after `DATA_RETENTION_DAYS` (default 30) finished
  deliveries, `/add` attempt records and subscribers who stopped or never opted in are deleted.
- Phone numbers are validated as Ghana mobile numbers (E.164 `+233…`) before any provider call.
- Delivery outcomes that may have been sent (`unknown`) are never retried automatically, to avoid
  duplicate messages.
- Event text is cleaned and length-limited, and only http(s) links survive normalisation.
- Before enabling SMS/WhatsApp workflows: implement channel-specific destination verification,
  opt-out, sending/cost caps and verification-code rate limits. Telegram consent does not grant
  permission to contact someone on another channel.

### API

- DRF denies any view that does not declare `permission_classes`. Health, approved sources,
  supported platforms and upcoming events are public. Source config and subscribers are private.
- Source submission requires session authentication/CSRF or a Telegram private-chat command.
  All submissions remain pending until reviewed, with a five-source daily limit per account/chat
  (the account limit is enforced under a row lock, so concurrent requests can't exceed it).
- Anonymous and authenticated requests are throttled. Throttle counts live in a PostgreSQL cache
  table shared by all processes. Behind a reverse proxy, set `API_NUM_PROXIES` so limits apply per
  client IP rather than to the proxy's address.
- With `DEBUG` off: HTTPS redirect, HSTS (1 hour by default, configurable), and secure cookies.

### Local development

- Docker Compose binds PostgreSQL and the API to `127.0.0.1`. The development database password is
  a default and must be changed for any shared or production deployment.
- The container runs as a non-root user.

### Production

- `docker-compose.prod.yml` runs gunicorn (no dev tools in the image), WhiteNoise for static
  files, and the worker, with restart policies. Required secrets fail fast if unset, and `web`
  listens on `127.0.0.1` only. TLS termination is left to a reverse proxy you provide; see
  [deployment](docs/deployment.md).

### Supply chain

- Dependencies are locked with hashes in `backend/uv.lock`, and CI audits them with `pip-audit`.
- GitHub Actions are pinned to commit SHAs, and workflows get a read-only token. Dependabot proposes updates.
