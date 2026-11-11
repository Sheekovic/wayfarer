import asyncio

import pytest

pytest.importorskip("playwright")

from wayfarer import (AsyncSession, ChallengeError, RenderError, RetryPolicy, RobotsDisallowed,
                      Session, WayfarerError)
from wayfarer.browser import proxy_settings

FAST = dict(delay=0, retry=RetryPolicy(backoff=0.01))


def test_proxy_settings():
    assert proxy_settings(None) is None
    assert proxy_settings("http://127.0.0.1:8080") == {"server": "http://127.0.0.1:8080"}
    assert proxy_settings("http://us%20er:p%40ss@proxy.test:3128") == {
        "server": "http://proxy.test:3128", "username": "us er", "password": "p@ss"}


def test_render_runs_javascript_and_shares_cookies(site):
    with Session(**FAST) as s:
        s.visit(site.url + "/set")                       # cookie set over HTTP
        page = s.render(site.url + "/js", wait_for="#done")
        assert page.status_code == 200
        assert page.title == "JS page"
        assert "rendered 42" in page.html
        assert s.page_url == site.url + "/js"
        sent = s.visit(site.url + "/echo").json()        # back on plain HTTP
        seen_by_browser = s.render(site.url + "/echo").html
    assert "jsid=42" in sent["cookie"] and "sid=abc123" in sent["cookie"]
    assert sent["referer"] == site.url + "/js"
    assert "sid=abc123" in seen_by_browser


def test_render_records_the_calls_a_page_makes(site):
    with Session(**FAST) as s:
        page = s.render(site.url + "/js", wait_for="#done")
        assert page.calls[0].resource_type == "document"
        (call,) = page.api_calls
        assert (call.method, call.status_code, call.content_type) == (
            "GET", 200, "application/json")
        assert call.url == site.url + "/echo?source=page"
        # the discovered endpoint works over plain HTTP, as the same visitor
        echoed = s.fetch(call.url).json()
    assert echoed["sec-fetch-mode"] == "cors"
    assert echoed["referer"] == site.url + "/js"
    assert "jsid=42" in echoed["cookie"]


def _closed_port_url():
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return f"http://127.0.0.1:{sock.getsockname()[1]}/"


def test_render_network_failure_is_a_render_error():
    with Session(**FAST) as s:
        with pytest.raises(RenderError) as info:
            s.render(_closed_port_url(), timeout=10)
        assert info.value.network
        assert s.page_url is None


def test_render_bad_selector_is_not_retried(site):
    with Session(**FAST) as s:
        with pytest.raises(RenderError) as info:
            s.render(site.url + "/js", wait_for="#never-there", timeout=1)
    assert not info.value.network
    assert len(site.hits("/js")) == 1


def test_render_reports_challenges(site):
    with Session(**FAST) as s:
        with pytest.raises(ChallengeError):
            s.render(site.url + "/cf")


def test_render_respects_robots(site):
    site.state["robots"] = "User-agent: *\nDisallow: /js\n"
    with Session(respect_robots=True, **FAST) as s:
        with pytest.raises(RobotsDisallowed):
            s.render(site.url + "/js")
    assert site.hits("/js") == []


def test_render_refuses_mobile_profiles(site):
    with Session("safari_ios", **FAST) as s:
        with pytest.raises(WayfarerError, match="mobile"):
            s.render(site.url + "/js")


def test_async_render(site):
    async def run():
        async with AsyncSession(**FAST) as s:
            page = await s.render(site.url + "/js", wait_for="#done")
            echo = (await s.fetch(site.url + "/echo")).json()
            return page, echo

    page, echo = asyncio.run(run())
    assert "rendered 42" in page.html
    assert "jsid=42" in echo["cookie"]
