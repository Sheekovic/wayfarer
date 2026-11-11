import asyncio
import time

import pytest

from wayfarer import AsyncSession, ChallengeError, ProxyPool, RetryPolicy, RobotsDisallowed, Session

FAST = dict(delay=0, retry=RetryPolicy(backoff=0.01))


def test_first_visit_looks_like_a_typed_navigation(site):
    with Session("chrome", **FAST) as s:
        h = s.visit(site.url + "/echo").json()
    assert h["sec-fetch-site"] == "none"
    assert h["sec-fetch-mode"] == "navigate"
    assert h["sec-fetch-dest"] == "document"
    assert h["sec-fetch-user"] == "?1"
    assert "referer" not in h
    assert "Chrome/" in h["user-agent"]
    assert h["accept-language"] == "en-US,en;q=0.9"


def test_second_visit_carries_referer(site):
    with Session(**FAST) as s:
        s.visit(site.url + "/page?x=1")
        assert s.page_url == site.url + "/page?x=1"
        h = s.visit(site.url + "/echo").json()
    assert h["referer"] == site.url + "/page?x=1"
    assert h["sec-fetch-site"] == "same-origin"


def test_fetch_looks_like_page_javascript(site):
    with Session(**FAST) as s:
        s.visit(site.url + "/page")
        h = s.fetch(site.url + "/echo").json()
        assert s.page_url == site.url + "/page"  # a fetch does not move the page
    assert h["accept"] == "*/*"
    assert h["sec-fetch-mode"] == "cors"
    assert h["sec-fetch-dest"] == "empty"
    assert h["sec-fetch-site"] == "same-origin"
    assert h["referer"] == site.url + "/page"
    # curl_cffi 0.17 stopped forwarding a caller-supplied Priority for Chrome
    # targets (it emulates stream priority itself). What wayfarer guarantees is
    # that it asks; tests/test_stealth.py pins the header we compute.
    assert h.get("priority", "u=1, i") == "u=1, i"
    assert "sec-fetch-user" not in h
    assert "upgrade-insecure-requests" not in h
    assert "origin" not in h


def test_json_post_is_a_fetch_with_origin(site):
    with Session(**FAST) as s:
        h = s.post(site.url + "/echo", json={"a": 1}).json()
    assert h["sec-fetch-mode"] == "cors"
    assert h["origin"] == site.url
    assert h["content-type"] == "application/json"


def test_user_headers_override_case_insensitively(site):
    with Session(languages=("fr-FR", "fr"), **FAST) as s:
        h = s.visit(site.url + "/echo").json()
        assert h["accept-language"] == "fr-FR,fr;q=0.9"
        h = s.visit(site.url + "/echo", headers={"accept-language": "ja", "X-Mine": "1"}).json()
    assert h["accept-language"] == "ja"
    assert h["x-mine"] == "1"


def test_firefox_user_agent_versions_agree(site):
    import re

    with Session("firefox", **FAST) as s:
        ua = s.visit(site.url + "/echo").json()["user-agent"]
        assert s.fetch(site.url + "/echo").json()["user-agent"] == ua
    rv, version = re.search(r"rv:(\d+)\.0\).*Firefox/(\d+)\.0$", ua).groups()
    assert rv == version


@pytest.mark.parametrize("profile,marker", [
    ("firefox", "Firefox/"), ("safari", "Safari/"),
    ("chrome_android", "Android"), ("safari_ios", "iPhone"),
])
def test_profiles_send_matching_user_agent(site, profile, marker):
    with Session(profile, **FAST) as s:
        h = s.visit(site.url + "/echo").json()
    assert marker in h["user-agent"]
    if profile == "chrome_android":
        assert h["sec-ch-ua-mobile"] == "?1"


def test_retries_then_succeeds(site):
    with Session(**FAST) as s:
        r = s.visit(site.url + "/flaky")
    assert r.status_code == 200 and r.text == "finally"
    assert len(site.hits("/flaky")) == 3


def test_post_is_not_retried(site):
    with Session(**FAST) as s:
        r = s.post(site.url + "/flaky", data={"a": "1"})
    assert r.status_code == 503
    assert len(site.hits("/flaky")) == 1


def test_challenge_raises_and_is_not_retried(site):
    with Session(**FAST) as s:
        with pytest.raises(ChallengeError) as info:
            s.visit(site.url + "/cf")
        assert s.page_url is None
    assert info.value.challenge.vendor == "Cloudflare"
    assert info.value.response.status_code == 403
    assert len(site.hits("/cf")) == 1


def test_challenge_can_be_ignored(site):
    with Session(on_challenge="ignore", **FAST) as s:
        assert s.visit(site.url + "/cf").status_code == 403


def test_pacing_delays_same_host(site):
    with Session(delay=0.3) as s:
        start = time.monotonic()
        s.visit(site.url + "/page")
        s.visit(site.url + "/page")
    assert time.monotonic() - start >= 0.3


def test_robots_respected_and_fetched_once(site):
    site.state["robots"] = "User-agent: *\nDisallow: /private\n"
    with Session(respect_robots=True, **FAST) as s:
        assert s.visit(site.url + "/page").status_code == 200
        with pytest.raises(RobotsDisallowed):
            s.visit(site.url + "/private/data")
    assert len(site.hits("/robots.txt")) == 1
    assert site.hits("/private/data") == []


def test_missing_robots_allows_everything(site):
    with Session(respect_robots=True, **FAST) as s:
        assert s.visit(site.url + "/private/data").status_code == 200


def test_cookies_persist_between_sessions(site, tmp_path):
    jar = tmp_path / "cookies.json"
    with Session(cookie_file=jar, **FAST) as s:
        s.visit(site.url + "/set")
    assert jar.exists()
    with Session(cookie_file=jar, **FAST) as s:
        h = s.visit(site.url + "/echo").json()
    assert h["cookie"] == "sid=abc123"


def test_session_proxy_carries_requests_and_browser_headers(site, proxy):
    with Session(proxy=proxy.url, **FAST) as s:
        s.visit(site.url + "/page")
        h = s.fetch(site.url + "/echo").json()
    assert [x["url"] for x in proxy.seen] == [site.url + "/page", site.url + "/echo"]
    assert h["sec-fetch-mode"] == "cors"
    assert h["referer"] == site.url + "/page"
    assert "proxy-authorization" not in h


def test_proxy_credentials_go_to_the_proxy_only(site, proxy):
    with Session(proxy=proxy.authed_url, **FAST) as s:
        h = s.visit(site.url + "/echo").json()
    assert proxy.seen[0]["auth"] == "alice:s3cret"
    assert "proxy-authorization" not in h


def test_per_request_proxy(site, proxy):
    with Session(**FAST) as s:
        s.visit(site.url + "/page")
        assert proxy.seen == []
        s.visit(site.url + "/page", proxy=proxy.url)
    assert len(proxy.seen) == 1


def test_async_session_proxy(site, proxy):
    async def run():
        async with AsyncSession(proxy=proxy.url, **FAST) as s:
            return (await s.visit(site.url + "/echo")).status_code

    assert asyncio.run(run()) == 200
    assert len(proxy.seen) == 1


def _dead_proxy():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}"


def test_pool_from_range_builds_encoded_urls():
    pool = ProxyPool.from_range("proxy.test", range(10000, 10003),
                                username="us er", password="p@ss:word")
    assert len(pool) == 3
    assert "http://us%20er:p%40ss%3Aword@proxy.test:10001" in pool
    assert "p%40ss" not in repr(pool)
    assert ProxyPool(["a", "b"]).pick(exclude={"a"}) == "b"
    with pytest.raises(ValueError):
        ProxyPool([])


def test_pooled_session_keeps_one_proxy(site, proxy):
    pool = ProxyPool([proxy.url, proxy.url.replace("127.0.0.1", "localhost")])
    with Session(proxy=pool, **FAST) as s:
        chosen = s.proxy
        for _ in range(4):
            s.visit(site.url + "/page")
        assert s.proxy == chosen
    assert len(proxy.seen) == 4


def test_pooled_session_fails_over_from_dead_proxy(site, proxy):
    dead = _dead_proxy()
    with Session(proxy=ProxyPool([dead, proxy.url]), timeout=5, **FAST) as s:
        s._core.proxy = dead
        assert s.visit(site.url + "/page").status_code == 200
        assert s.proxy == proxy.url


def test_single_dead_proxy_raises(site):
    with Session(proxy=_dead_proxy(), timeout=5, **FAST) as s:
        with pytest.raises(OSError):
            s.visit(site.url + "/page")
    assert site.hits("/page") == []


def test_async_session(site):
    async def run():
        async with AsyncSession("firefox", respect_robots=True, **FAST) as s:
            await s.visit(site.url + "/page")
            nav, api = await asyncio.gather(
                s.visit(site.url + "/flaky"),
                s.fetch(site.url + "/echo"),
            )
            with pytest.raises(ChallengeError):
                await s.visit(site.url + "/cf")
            return nav, api.json()

    nav, h = asyncio.run(run())
    assert nav.text == "finally"
    assert "Firefox/" in h["user-agent"]
    assert h["sec-fetch-mode"] == "cors"
    assert h["priority"] == "u=4"
    assert len(site.hits("/robots.txt")) == 1
