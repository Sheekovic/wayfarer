"""Checks that need a real browser and a network, so they are opt-in.

Run them with ``WAYFARER_LIVE=1 python -m pytest -m live``. They are the part
of the suite that can lie if left out, because a fingerprint fix that silently
stops landing looks exactly like a working one until a site disagrees.
"""

import os

import pytest

from wayfarer import Persona, Session, diagnose, lint
from wayfarer.detect import collect_signals
from wayfarer.stealth import ARTIFACT, camoufox_version, require_camoufox

def live(fn):
    """A check that needs a real browser: runs only with WAYFARER_LIVE=1."""
    skip = pytest.mark.skipif(
        not os.environ.get("WAYFARER_LIVE"),
        reason="set WAYFARER_LIVE=1 to run the browser-and-network checks",
    )
    return pytest.mark.live(skip(fn))

LOCAL_PAGE = "about:blank"


def signals_for(session, url=LOCAL_PAGE):
    session.render(url, wait_until="domcontentloaded", timeout=90)
    return session.signals


@live
def test_vanilla_playwright_admits_it_is_automated():
    """The honest browser: this is the baseline stealth is measured against."""
    reports = diagnose(["playwright"], timeout=90)
    signals = reports[0].signals
    assert signals, "the probe read nothing"
    assert signals["webdriver"] is True or signals["headlessUa"], \
        "an unmodified Playwright browser stopped looking automated -- recheck the baseline"


@live
def test_camoufox_does_not_admit_it_is_automated():
    require_camoufox()
    reports = diagnose(["camoufox"], timeout=120)
    signals, leaks = reports[0].signals, lint(reports[0].signals)
    assert signals, "the probe read nothing"
    codes = {leak.code for leak in leaks}
    assert "webdriver_flag" not in codes, f"navigator.webdriver is true: {signals['webdriver']}"
    assert "headless_user_agent" not in codes, signals["userAgent"]
    assert not signals["jugglerGlobals"], signals["jugglerGlobals"]


@live
def test_the_gpu_string_wayfarer_rewrites_actually_lands():
    """The point of patch_webgl, checked where a detector would read it."""
    require_camoufox()
    with Session("firefox", stealth=True, persona=Persona.new(), on_challenge="ignore") as s:
        signals = signals_for(s)
    renderer = signals.get("webglRenderer") or ""
    assert renderer, "no WebGL renderer reported"
    assert ARTIFACT not in renderer, f"Camoufox's hedge survived the rewrite: {renderer}"
    assert "webgl_vague_gpu" not in {leak.code for leak in lint(signals)}


@live
def test_the_identity_survives_a_restart():
    """A saved persona must present the same browser twice, or continuity is a claim."""
    require_camoufox()
    persona = Persona.new()
    with Session("firefox", stealth=True, persona=persona, on_challenge="ignore") as s:
        first = signals_for(s)
    again = Persona.from_dict(persona.to_dict())
    with Session("firefox", stealth=True, persona=again, on_challenge="ignore") as s:
        second = signals_for(s)
    assert first["userAgent"] == second["userAgent"]
    assert first["webglRenderer"] == second["webglRenderer"]
    assert first["screen"] == second["screen"]


@live
def test_a_rendered_page_still_carries_the_session_cookies(site):
    with Session("firefox", stealth=True, persona=Persona.new(), delay=0,
                 on_challenge="ignore") as s:
        s.render(site.url + "/js", wait_for="#done", timeout=90)
        served = s.visit(site.url + "/echo").json()
    assert "jsid=42" in served.get("cookie", "")


@live
def test_camoufox_is_reported_to_the_user():
    assert camoufox_version()
