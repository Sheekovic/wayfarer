from types import SimpleNamespace

import pytest

from wayfarer import HostPacer, RetryPolicy, detect_challenge
from wayfarer.headers import (
    FETCH,
    NAVIGATE,
    build_headers,
    origin,
    referer_for,
    registrable_domain,
    site_relation,
)
from wayfarer.pacing import parse_retry_after
from wayfarer.profiles import get_profile
from wayfarer.robots import RobotsCache

LANGS = ("en-US", "en")


def test_origin_drops_default_port_and_userinfo():
    assert origin("HTTPS://user:pw@Example.com:443/a?b#c") == "https://example.com"
    assert origin("http://example.com:8080/") == "http://example.com:8080"


def test_registrable_domain():
    assert registrable_domain("a.b.example.com") == "example.com"
    assert registrable_domain("shop.example.co.uk") == "example.co.uk"
    assert registrable_domain("127.0.0.1") == "127.0.0.1"


@pytest.mark.parametrize(
    "initiator,target,expected",
    [
        (None, "https://example.com/", "none"),
        ("https://example.com/a", "https://example.com/b", "same-origin"),
        ("https://www.example.com/", "https://api.example.com/", "same-site"),
        ("http://www.example.com/", "https://www.example.com/", "cross-site"),
        ("https://example.com/", "https://other.org/", "cross-site"),
    ],
)
def test_site_relation(initiator, target, expected):
    assert site_relation(initiator, target) == expected


def test_referer_policy():
    page = "https://example.com/a/b?q=1#frag"
    assert referer_for(page, "https://example.com/c") == "https://example.com/a/b?q=1"
    assert referer_for(page, "https://other.org/") == "https://example.com/"
    assert referer_for(page, "http://other.org/") is None


def test_accept_language_per_family():
    langs = ("de-DE", "de", "en-US", "en")
    assert get_profile("chrome").accept_language(langs) == "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7"
    assert get_profile("firefox").accept_language(langs) == "de-DE,de;q=0.8,en-US;q=0.5,en;q=0.3"
    assert get_profile("firefox").accept_language(LANGS) == "en-US,en;q=0.5"


def test_unknown_profile():
    with pytest.raises(ValueError, match="unknown profile"):
        get_profile("netscape")


def test_first_navigation_headers():
    h = build_headers(get_profile("chrome"), mode=NAVIGATE, method="GET",
                      url="https://example.com/", page_url=None, languages=LANGS)
    assert h["Sec-Fetch-Site"] == "none"
    assert "Referer" not in h and "Origin" not in h


def test_fetch_headers_cross_site():
    h = build_headers(get_profile("chrome"), mode=FETCH, method="GET",
                      url="https://api.other.org/v1", page_url="https://example.com/app",
                      languages=LANGS)
    assert h["Sec-Fetch-Site"] == "cross-site"
    assert h["Sec-Fetch-Mode"] == "cors"
    assert h["Sec-Fetch-Dest"] == "empty"
    assert h["Sec-Fetch-User"] is None
    assert h["Origin"] == "https://example.com"
    assert h["Referer"] == "https://example.com/"
    assert h["Priority"] == "u=1, i"


def test_safari_headers_are_lowercase():
    h = build_headers(get_profile("safari"), mode=FETCH, method="GET",
                      url="https://example.com/x", page_url=None, languages=LANGS)
    assert all(k == k.lower() for k in h)
    assert "origin" not in h  # same-origin GET


def test_pacer_spaces_requests_per_host():
    now = [100.0]
    pacer = HostPacer(2.0, clock=lambda: now[0])
    assert pacer.reserve("a.test") == 0
    assert pacer.reserve("a.test") == 2.0
    assert pacer.reserve("b.test") == 0
    now[0] += 10
    assert pacer.reserve("a.test") == 0


def test_pacer_floor_overrides_delay():
    pacer = HostPacer(0, clock=lambda: 0.0)
    pacer.set_floor("a.test", 5)
    pacer.reserve("a.test")
    assert pacer.reserve("a.test") == 5


def test_parse_retry_after():
    assert parse_retry_after("7") == 7.0
    assert parse_retry_after(None) is None
    assert parse_retry_after("soon") is None
    assert parse_retry_after("Wed, 21 Oct 2015 07:28:00 GMT") == 0.0


def test_retry_policy():
    policy = RetryPolicy(attempts=3, backoff=1.0)
    assert policy.wait_before_retry(1, "GET", status=200) is None
    assert policy.wait_before_retry(1, "POST", status=503) is None
    assert policy.wait_before_retry(3, "GET", status=503) is None
    assert 0.5 <= policy.wait_before_retry(1, "GET", status=503) <= 1.0
    assert 1.0 <= policy.wait_before_retry(2, "GET") <= 2.0
    assert policy.wait_before_retry(1, "GET", status=429, retry_after="4") == 4.0
    assert policy.wait_before_retry(1, "GET", status=429, retry_after="3600") is None


def _resp(status, body=b"", **headers):
    return SimpleNamespace(status_code=status, content=body, headers=headers)


def test_detect_challenge():
    assert detect_challenge(_resp(200, b"<html>hello</html>")) is None
    assert detect_challenge(_resp(403, b"plain forbidden")) is None
    assert detect_challenge(_resp(200, **{"cf-mitigated": "challenge"})).vendor == "Cloudflare"
    cf = detect_challenge(_resp(503, b"<title>Just a moment...</title>", server="cloudflare"))
    assert (cf.vendor, cf.kind) == ("Cloudflare", "challenge")
    assert detect_challenge(_resp(403, b"geo.captcha-delivery.com")).vendor == "DataDome"
    assert detect_challenge(_resp(403, b'<div class="g-recaptcha">')).vendor == "CAPTCHA"
    # A normal page that merely embeds a CAPTCHA widget is not a challenge.
    assert detect_challenge(_resp(200, b'<div class="g-recaptcha">')) is None


def test_robots_cache():
    cache = RobotsCache()
    url = "https://example.com/private/x"
    assert cache.allowed(url) is None
    cache.store(url, 200, "User-agent: *\nDisallow: /private\nCrawl-delay: 3\n")
    assert cache.allowed(url) is False
    assert cache.allowed("https://example.com/public") is True
    assert cache.crawl_delay(url) == 3.0
    cache.store("https://gone.test/", 404)
    assert cache.allowed("https://gone.test/x") is True
    cache.store("https://down.test/", None)
    assert cache.allowed("https://down.test/x") is False
