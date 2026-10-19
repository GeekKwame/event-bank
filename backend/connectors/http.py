"""SSRF-hardened HTTP fetching for connectors.

Every request (including each redirect hop) is checked against a `FetchPolicy`:
https only, allowlisted hosts and ports, no credentials or IP literals in the
URL, and every DNS answer must be a public address. The connection is then
made to the vetted IP itself (with the original Host header and TLS SNI), so a
second DNS lookup cannot swap in a different address (DNS rebinding).
"""

import ipaddress
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from connectors.base import FetchResponse

Resolver = Callable[[str, int], Sequence[str]]
Clock = Callable[[], float]

REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
DEFAULT_ACCEPTED_CONTENT_TYPES = frozenset(
    {"text/html", "application/xhtml+xml", "application/ld+json", "application/json"}
)
_NAT64_NETWORKS = (
    ipaddress.ip_network("64:ff9b::/96"),
    ipaddress.ip_network("64:ff9b:1::/48"),
)


class FetchError(Exception):
    code = "fetch_error"


class UnsafeURLError(FetchError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class FetchTimeoutError(FetchError):
    code = "timeout"


class FetchConnectionError(FetchError):
    code = "connection_error"


class TooManyRedirectsError(FetchError):
    code = "too_many_redirects"


class ResponseTooLargeError(FetchError):
    code = "response_too_large"


class UnexpectedContentError(FetchError):
    code = "unexpected_content"


class HTTPStatusError(FetchError):
    code = "http_status"

    def __init__(self, status_code: int, url: str):
        super().__init__(f"HTTP {status_code} from {url}")
        self.status_code = status_code


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: frozenset[str]
    allowed_ports: frozenset[int] = frozenset({443})
    max_redirects: int = 3
    max_response_bytes: int = 2 * 1024 * 1024
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    total_timeout: float = 20.0
    user_agent: str = "EventBankBot/0.1"
    accepted_content_types: frozenset[str] = DEFAULT_ACCEPTED_CONTENT_TYPES


@dataclass(frozen=True)
class ValidatedTarget:
    url: httpx.URL
    address: str

    @property
    def pinned_url(self) -> httpx.URL:
        return self.url.copy_with(host=self.address)

    @property
    def ascii_host(self) -> str:
        return self.url.raw_host.decode("ascii")


def display_url(url: str | httpx.URL) -> str:
    """The URL without userinfo, query string or fragment, which may carry secrets."""
    try:
        parsed = httpx.URL(str(url))
    except (httpx.InvalidURL, TypeError):
        return "<invalid url>"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme}://{parsed.host}{port}{parsed.path}"


def is_public_address(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return is_public_address(str(ip.ipv4_mapped))
        if ip.sixtofour is not None:
            return is_public_address(str(ip.sixtofour))
        if ip.teredo is not None or any(ip in network for network in _NAT64_NETWORKS):
            return False
    return ip.is_global and not ip.is_multicast


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    addresses: list[str] = []
    for *_, sockaddr in infos:
        if sockaddr[0] not in addresses:
            addresses.append(sockaddr[0])
    return addresses


def check_url(url: str, policy: FetchPolicy | None = None) -> httpx.URL:
    """Syntax, scheme, host and port checks that need no DNS lookup.

    With no policy the host allowlist and port checks are skipped.
    """
    if any(char.isspace() or ord(char) < 0x20 or char == "\\" for char in url):
        raise UnsafeURLError(
            "invalid_url", "URL contains whitespace, control or backslash characters"
        )
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise UnsafeURLError("invalid_url", "URL could not be parsed") from None
    if parsed.scheme != "https":
        raise UnsafeURLError("unsupported_scheme", "Only https:// URLs are allowed")
    if parsed.userinfo:
        raise UnsafeURLError("credentials_in_url", "URLs must not contain credentials")
    if not parsed.host:
        raise UnsafeURLError("invalid_url", "URL has no host")
    try:
        ipaddress.ip_address(parsed.host)
    except ValueError:
        pass
    else:
        raise UnsafeURLError(
            "ip_literal_not_allowed", "URLs must use a hostname, not an IP address"
        )
    if policy is not None:
        if parsed.host not in policy.allowed_hosts:
            raise UnsafeURLError("unsupported_host", f"Host {parsed.host!r} is not allowed")
        if (parsed.port or 443) not in policy.allowed_ports:
            raise UnsafeURLError("unsupported_port", f"Port {parsed.port} is not allowed")
    return parsed


def validate_url(
    url: str, policy: FetchPolicy, *, resolver: Resolver = system_resolver
) -> ValidatedTarget:
    parsed = check_url(url, policy)
    host = parsed.raw_host.decode("ascii")
    try:
        addresses = list(resolver(host, parsed.port or 443))
    except OSError:
        raise UnsafeURLError("dns_resolution_failed", f"Could not resolve {host!r}") from None
    if not addresses:
        raise UnsafeURLError("dns_resolution_failed", f"Could not resolve {host!r}")
    if not all(is_public_address(address) for address in addresses):
        raise UnsafeURLError("non_public_address", f"{host!r} resolves to a non-public address")
    return ValidatedTarget(url=parsed, address=addresses[0])


def _charset(content_type: str) -> str | None:
    for param in content_type.split(";")[1:]:
        key, _, value = param.partition("=")
        if key.strip().lower() == "charset":
            return value.strip().strip('"') or None
    return None


class SafeFetcher:
    def __init__(
        self,
        policy: FetchPolicy,
        *,
        resolver: Resolver = system_resolver,
        transport: httpx.BaseTransport | None = None,
        clock: Clock = time.monotonic,
    ):
        self.policy = policy
        self._resolver = resolver
        self._clock = clock
        # No keep-alive: a pooled connection is keyed by IP and could otherwise be reused for a
        # different hostname. trust_env=False stops proxy env vars from bypassing IP pinning.
        self._client = httpx.Client(
            transport=transport
            or httpx.HTTPTransport(limits=httpx.Limits(max_keepalive_connections=0), retries=0),
            follow_redirects=False,
            trust_env=False,
        )

    def __enter__(self) -> "SafeFetcher":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get(self, url: str) -> FetchResponse:
        deadline = self._clock() + self.policy.total_timeout
        current = url
        for _ in range(self.policy.max_redirects + 1):
            target = validate_url(current, self.policy, resolver=self._resolver)
            status, headers, body = self._request(target, deadline)
            if status in REDIRECT_STATUSES:
                location = headers.get("location")
                if not location:
                    raise HTTPStatusError(status, display_url(target.url))
                current = urljoin(str(target.url), location)
                continue
            return self._build_response(target, status, headers, body)
        raise TooManyRedirectsError(
            f"More than {self.policy.max_redirects} redirects starting at {display_url(url)}"
        )

    def _remaining(self, deadline: float, target: ValidatedTarget) -> float:
        remaining = deadline - self._clock()
        if remaining <= 0:
            raise FetchTimeoutError(f"Fetch deadline exceeded for {display_url(target.url)}")
        return remaining

    def _request(
        self, target: ValidatedTarget, deadline: float
    ) -> tuple[int, Mapping[str, str], bytes]:
        policy = self.policy
        shown = display_url(target.url)
        remaining = self._remaining(deadline, target)
        timeout = httpx.Timeout(
            connect=min(policy.connect_timeout, remaining),
            read=min(policy.read_timeout, remaining),
            write=min(policy.read_timeout, remaining),
            pool=min(policy.connect_timeout, remaining),
        )
        self._client.cookies.clear()
        request = self._client.build_request(
            "GET",
            target.pinned_url,
            headers={
                "Host": target.url.netloc.decode("ascii"),
                "User-Agent": policy.user_agent,
                "Accept": ", ".join(sorted(policy.accepted_content_types)),
                "Accept-Encoding": "identity",
            },
            timeout=timeout,
            extensions={"sni_hostname": target.ascii_host},
        )
        try:
            response = self._client.send(request, stream=True)
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(f"Timed out connecting to {shown}") from exc
        except httpx.HTTPError as exc:
            raise FetchConnectionError(f"Could not fetch {shown}: {type(exc).__name__}") from exc

        try:
            if response.status_code in REDIRECT_STATUSES:
                return response.status_code, response.headers, b""
            if not 200 <= response.status_code < 300:
                raise HTTPStatusError(response.status_code, shown)
            encoding = response.headers.get("content-encoding", "identity").strip().lower()
            if encoding not in ("", "identity"):
                raise UnexpectedContentError(f"Unsupported content encoding {encoding!r}")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > policy.max_response_bytes:
                raise ResponseTooLargeError(f"Response from {shown} exceeds the size limit")
            body = bytearray()
            # Non-identity encodings were rejected above, so these are the bytes on the wire.
            for chunk in response.iter_bytes():
                body += chunk
                if len(body) > policy.max_response_bytes:
                    raise ResponseTooLargeError(f"Response from {shown} exceeds the size limit")
                if self._clock() > deadline:
                    raise FetchTimeoutError(f"Fetch deadline exceeded for {shown}")
            return response.status_code, response.headers, bytes(body)
        except httpx.TimeoutException as exc:
            raise FetchTimeoutError(f"Timed out reading {shown}") from exc
        except httpx.HTTPError as exc:
            raise FetchConnectionError(f"Could not read {shown}: {type(exc).__name__}") from exc
        finally:
            response.close()

    def _build_response(
        self, target: ValidatedTarget, status: int, headers: Mapping[str, str], body: bytes
    ) -> FetchResponse:
        content_type = headers.get("content-type", "")
        mime = content_type.split(";")[0].strip().lower()
        if mime not in self.policy.accepted_content_types:
            raise UnexpectedContentError(f"Unexpected content type {mime or 'missing'!r}")
        charset = _charset(content_type) or "utf-8"
        try:
            text = body.decode(charset, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")
        return FetchResponse(url=str(target.url), status_code=status, content_type=mime, text=text)
