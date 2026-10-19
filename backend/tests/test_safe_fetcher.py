import itertools

import httpx
import pytest

from connectors.http import (
    FetchConnectionError,
    FetchPolicy,
    FetchTimeoutError,
    HTTPStatusError,
    ResponseTooLargeError,
    SafeFetcher,
    TooManyRedirectsError,
    UnexpectedContentError,
    UnsafeURLError,
    display_url,
    is_public_address,
    validate_url,
)
from tests.conftest import PUBLIC_IP, public_resolver

HOST = "events.example.org"
POLICY = FetchPolicy(allowed_hosts=frozenset({HOST, "cdn.example.org"}), max_response_bytes=1000)


def make_fetcher(handler, *, resolver=public_resolver, policy=POLICY, clock=None):
    kwargs = {"clock": clock} if clock else {}
    return SafeFetcher(policy, resolver=resolver, transport=httpx.MockTransport(handler), **kwargs)


def html(body="<html></html>", **kwargs):
    return httpx.Response(200, html=body, **kwargs)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("http://events.example.org/", "unsupported_scheme"),
        ("ftp://events.example.org/", "unsupported_scheme"),
        ("file:///etc/passwd", "unsupported_scheme"),
        ("https://user:pass@events.example.org/", "credentials_in_url"),
        ("https://127.0.0.1/", "ip_literal_not_allowed"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal_not_allowed"),
        ("https://[::1]/", "ip_literal_not_allowed"),
        ("https://10.0.0.5/", "ip_literal_not_allowed"),
        ("https://localhost/", "unsupported_host"),
        ("https://metadata.google.internal/", "unsupported_host"),
        ("https://evil.example.com/", "unsupported_host"),
        ("https://events.example.org.evil.com/", "unsupported_host"),
        ("https://events.example.org:8443/", "unsupported_port"),
        ("https://events.example.org/a b", "invalid_url"),
        ("https://events.example.org\\@evil.com/", "invalid_url"),
    ],
)
def test_rejects_unsafe_urls_before_dns(url, code):
    def resolver(host, port):
        raise AssertionError("DNS must not be queried for a URL rejected on syntax")

    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(url, POLICY, resolver=resolver)
    assert excinfo.value.code == code


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.100.100.200",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fd00:ec2::254",
        "::ffff:127.0.0.1",
        "::ffff:169.254.169.254",
        "2002:7f00:0001::1",
        "64:ff9b::7f00:1",
        "2001:db8::1",
    ],
)
def test_non_public_addresses_are_rejected(address):
    assert not is_public_address(address)
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(f"https://{HOST}/", POLICY, resolver=lambda host, port: [address])
    assert excinfo.value.code == "non_public_address"


def test_any_non_public_answer_rejects_the_host():
    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(
            f"https://{HOST}/", POLICY, resolver=lambda host, port: [PUBLIC_IP, "10.0.0.1"]
        )
    assert excinfo.value.code == "non_public_address"


@pytest.mark.parametrize("answer", [[], OSError("no such host")])
def test_dns_failures_are_rejected(answer):
    def resolver(host, port):
        if isinstance(answer, Exception):
            raise answer
        return answer

    with pytest.raises(UnsafeURLError) as excinfo:
        validate_url(f"https://{HOST}/", POLICY, resolver=resolver)
    assert excinfo.value.code == "dns_resolution_failed"


def test_public_addresses_are_accepted():
    assert is_public_address(PUBLIC_IP)
    assert is_public_address("2606:4700::1111")


def test_connects_to_the_vetted_ip_with_original_host_and_sni():
    seen = []

    def handler(request):
        seen.append(request)
        return html("<p>ok</p>")

    with make_fetcher(handler) as fetcher:
        response = fetcher.get(f"https://{HOST}/calendar?x=1")

    request = seen[0]
    assert request.url.host == PUBLIC_IP
    assert request.url.raw_path == b"/calendar?x=1"
    assert request.headers["Host"] == HOST
    assert request.extensions["sni_hostname"] == HOST
    assert request.headers["Accept-Encoding"] == "identity"
    assert "cookie" not in request.headers
    assert response.url == f"https://{HOST}/calendar?x=1"
    assert response.text == "<p>ok</p>"


def test_dns_rebinding_cannot_change_the_connected_address():
    answers = iter([[PUBLIC_IP], ["127.0.0.1"]])
    calls = []

    def rebinding_resolver(host, port):
        calls.append(host)
        return next(answers)

    seen = []

    def handler(request):
        seen.append(request.url.host)
        return html()

    with make_fetcher(handler, resolver=rebinding_resolver) as fetcher:
        fetcher.get(f"https://{HOST}/")

    assert calls == [HOST]
    assert seen == [PUBLIC_IP]


def test_follows_safe_redirects_and_revalidates_each_hop():
    resolved = []

    def resolver(host, port):
        resolved.append(host)
        return [PUBLIC_IP]

    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://cdn.example.org/next"})
        if request.url.path == "/next":
            return httpx.Response(301, headers={"Location": "/final"})
        return html("done")

    with make_fetcher(handler, resolver=resolver) as fetcher:
        response = fetcher.get(f"https://{HOST}/start")

    assert response.text == "done"
    assert response.url == "https://cdn.example.org/final"
    assert resolved == [HOST, "cdn.example.org", "cdn.example.org"]


@pytest.mark.parametrize(
    ("location", "code"),
    [
        ("http://events.example.org/insecure", "unsupported_scheme"),
        ("https://169.254.169.254/latest/meta-data/", "ip_literal_not_allowed"),
        ("https://localhost/admin", "unsupported_host"),
        ("https://attacker.example.net/", "unsupported_host"),
    ],
)
def test_rejects_redirects_to_unsafe_destinations(location, code):
    def handler(request):
        return httpx.Response(302, headers={"Location": location})

    with make_fetcher(handler) as fetcher, pytest.raises(UnsafeURLError) as excinfo:
        fetcher.get(f"https://{HOST}/")
    assert excinfo.value.code == code


def test_rejects_redirect_to_allowed_host_that_resolves_privately():
    def resolver(host, port):
        return ["10.0.0.7"] if host == "cdn.example.org" else [PUBLIC_IP]

    def handler(request):
        return httpx.Response(302, headers={"Location": "https://cdn.example.org/"})

    with make_fetcher(handler, resolver=resolver) as fetcher, pytest.raises(UnsafeURLError) as e:
        fetcher.get(f"https://{HOST}/")
    assert e.value.code == "non_public_address"


def test_limits_redirect_count():
    counter = itertools.count()

    def handler(request):
        return httpx.Response(302, headers={"Location": f"/hop{next(counter)}"})

    with make_fetcher(handler) as fetcher, pytest.raises(TooManyRedirectsError):
        fetcher.get(f"https://{HOST}/")
    assert next(counter) == POLICY.max_redirects + 1


def test_rejects_oversized_response_by_content_length():
    def handler(request):
        return httpx.Response(
            200, headers={"Content-Type": "text/html", "Content-Length": "5000"}, content=b"x"
        )

    with make_fetcher(handler) as fetcher, pytest.raises(ResponseTooLargeError):
        fetcher.get(f"https://{HOST}/")


def test_rejects_oversized_streamed_response_without_content_length():
    def chunks():
        for _ in range(100):
            yield b"x" * 100

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=chunks())

    with make_fetcher(handler) as fetcher, pytest.raises(ResponseTooLargeError):
        fetcher.get(f"https://{HOST}/")


@pytest.mark.parametrize(
    "exc", [httpx.ConnectTimeout("slow"), httpx.ReadTimeout("slow"), httpx.PoolTimeout("slow")]
)
def test_timeouts_raise_fetch_timeout(exc):
    def handler(request):
        raise exc

    with make_fetcher(handler) as fetcher, pytest.raises(FetchTimeoutError):
        fetcher.get(f"https://{HOST}/")


def test_total_deadline_is_enforced_while_streaming():
    ticks = itertools.count(start=0, step=5)

    def chunks():
        for _ in range(10):
            yield b"x"

    def handler(request):
        return httpx.Response(200, headers={"Content-Type": "text/html"}, content=chunks())

    with make_fetcher(handler, clock=lambda: next(ticks)) as fetcher:
        with pytest.raises(FetchTimeoutError):
            fetcher.get(f"https://{HOST}/")


def test_timeouts_passed_to_transport_never_exceed_remaining_budget():
    seen = []

    def handler(request):
        seen.append(request.extensions["timeout"])
        return html()

    policy = FetchPolicy(allowed_hosts=frozenset({HOST}), total_timeout=2.0)
    with make_fetcher(handler, policy=policy) as fetcher:
        fetcher.get(f"https://{HOST}/")
    assert all(value <= 2.0 for value in seen[0].values())


@pytest.mark.parametrize("status", [404, 500, 503])
def test_http_errors_raise_http_status_error(status):
    with make_fetcher(lambda request: httpx.Response(status)) as fetcher:
        with pytest.raises(HTTPStatusError) as excinfo:
            fetcher.get(f"https://{HOST}/")
    assert excinfo.value.status_code == status


def test_connection_errors_raise_fetch_connection_error():
    def handler(request):
        raise httpx.ConnectError("refused")

    with make_fetcher(handler) as fetcher, pytest.raises(FetchConnectionError):
        fetcher.get(f"https://{HOST}/")


def test_rejects_unexpected_content_type_and_encoding():
    with make_fetcher(lambda request: httpx.Response(200, content=b"\x89PNG")) as fetcher:
        with pytest.raises(UnexpectedContentError):
            fetcher.get(f"https://{HOST}/")

    def gzipped(request):
        return httpx.Response(
            200, headers={"Content-Type": "text/html", "Content-Encoding": "gzip"}, content=b"x"
        )

    with make_fetcher(gzipped) as fetcher, pytest.raises(UnexpectedContentError):
        fetcher.get(f"https://{HOST}/")


def test_does_not_send_cookies_set_by_previous_responses():
    seen = []

    def handler(request):
        seen.append(request.headers.get("cookie"))
        return html(headers={"Set-Cookie": "session=abc"})

    with make_fetcher(handler) as fetcher:
        fetcher.get(f"https://{HOST}/one")
        fetcher.get(f"https://{HOST}/two")
    assert seen == [None, None]


def test_decodes_declared_charset():
    def handler(request):
        return httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=iso-8859-1"},
            content="café".encode("iso-8859-1"),
        )

    with make_fetcher(handler) as fetcher:
        assert fetcher.get(f"https://{HOST}/").text == "café"


def test_display_url_hides_query_and_credentials():
    assert (
        display_url("https://user:pw@api.example.org/path?key=secret#frag")
        == "https://api.example.org/path"
    )


def test_default_client_ignores_proxy_environment(monkeypatch):
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:3128")
    fetcher = SafeFetcher(POLICY, resolver=public_resolver)
    try:
        assert fetcher._client._trust_env is False
    finally:
        fetcher.close()
