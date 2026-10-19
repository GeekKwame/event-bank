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

- Luma is accessed only through its official API, as its Terms of Use require. No luma.com pages are
  scraped, and the test fixtures are synthetic.
- Only public events are stored. Guest-only addresses are reduced to the city. Meeting links and
  submitter details (including emails) are never stored.

### Notification abuse

- Real SMS sending is off unless `SMS_SENDING_ENABLED=true`.
- Phone numbers are validated as Ghana mobile numbers (E.164 `+233…`) before any provider call.
- Delivery outcomes that may have been sent (`unknown`) are never retried automatically, to avoid
  duplicate messages.
- Event text is cleaned and length-limited, and only http(s) links survive normalisation.
- Not yet implemented (Phase 3–4, required before live sending): double opt-in consent, STOP/opt-out,
  per-recipient and global sending caps, and verification-code rate limits.

### API

- DRF denies any view that does not declare `permission_classes`. Only `/api/health/` is public.
- Anonymous and authenticated requests are throttled.
- With `DEBUG` off: HTTPS redirect, HSTS (1 hour by default, configurable), and secure cookies.

### Local development

- Docker Compose binds PostgreSQL and the API to `127.0.0.1`. The development database password is
  a default and must be changed for any shared or production deployment.
- The container runs as a non-root user.
- A hardened production deployment (WSGI server, static files, TLS termination) is not provided yet.

### Supply chain

- Dependencies are locked with hashes in `backend/uv.lock`, and CI audits them with `pip-audit`.
- GitHub Actions are pinned to commit SHAs, and workflows get a read-only token. Dependabot proposes updates.
