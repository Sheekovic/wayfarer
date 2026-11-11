"""Checks that need a real browser and a network, so they are opt-in.

Run them with ``WAYFARER_LIVE=1 python -m pytest -m live``. They are the part
of the suite that can lie if left out, because a fingerprint fix that silently
stops landing looks exactly like a working one until a site disagrees.
"""

import os

import pytest

from wayfarer import Persona, Session, diagnose, lint
from wayfarer.detect import fingerprint_draws, load_reports
from wayfarer.stealth import ARTIFACT, camoufox_version, require_camoufox

def live(fn):
    """A check that needs a real browser: runs only with WAYFARER_LIVE=1."""
    skip = pytest.mark.skipif(
        not os.environ.get("WAYFARER_LIVE"),
        reason="set WAYFARER_LIVE=1 to run the browser-and-network checks",
    )
    return pytest.mark.live(skip(fn))

LOCAL_PAGE = "about:blank"
BOT_TEST_PAGE = "https://bot.sannysoft.com/"


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
    """The point of patch_webgl, checked where a detector would read it.

    Two things can go wrong here and both matter: the rewrite stops landing (the
    hedge reappears), or WebGL goes away entirely -- headless Firefox on a
    machine with no reachable GPU does that, and a desktop browser with no
    WebGL is its own tell. So the test pins the string when there is one, and
    pins that wayfarer *notices* when there is not.
    """
    require_camoufox()
    with Session("firefox", stealth=True, persona=Persona.new(), on_challenge="ignore") as s:
        signals = signals_for(s)
    renderer = signals.get("webglRenderer") or ""
    leaks = {leak.code for leak in lint(signals)}
    if not renderer:
        assert "webgl_missing" in leaks, "a visitor with no WebGL and nobody saying so"
        pytest.skip("this machine gave the headless browser no WebGL; re-run headed to "
                    "check the rewritten renderer string")
    assert ARTIFACT not in renderer, f"Camoufox's hedge survived the rewrite: {renderer}"
    assert "webgl_vague_gpu" not in leaks


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
    for key in ("userAgent", "webglVendor", "webglRenderer", "screen", "timezone",
                "hardwareConcurrency", "platform"):
        assert first[key] == second[key], f"{key} changed across launches: {first[key]!r} -> {second[key]!r}"


@live
def test_draws_that_should_be_stable_are_stable():
    """What a site can actually use to recognise this browser again.

    Canvas is deliberately not asserted: Camoufox 0.5.9 re-randomises canvas
    readback on each launch, so it is a per-launch value. The audio and WebGL
    parameter draws are the ones that carry across, and that is what this pins.
    """
    require_camoufox()
    draws = {}

    def look(page, snapshot):
        draws.update(fingerprint_draws(page))

    persona = Persona.new()
    with Session("firefox", stealth=True, persona=persona, on_challenge="ignore") as s:
        s.render("about:blank", wait_until="domcontentloaded", timeout=120, on_page=look)
        first = dict(draws)
        draws.clear()
        s.render("about:blank", wait_until="domcontentloaded", timeout=120, on_page=look)
        second = dict(draws)
    assert set(first) >= {"canvas", "audio", "webglParams"}
    assert first["audio"] == second["audio"] and first["webglParams"] == second["webglParams"], \
        "within one browser, the same draws should come out the same"


@live
def test_a_rendered_page_still_carries_the_session_cookies(site):
    with Session("firefox", stealth=True, persona=Persona.new(), delay=0,
                 on_challenge="ignore") as s:
        s.render(site.url + "/js", wait_for="#done", timeout=90)
        served = s.visit(site.url + "/echo").json()
    assert "jsid=42" in served.get("cookie", "")


@live
def test_a_public_bot_test_page_adds_no_new_hard_leaks(tmp_path):
    """bot.sannysoft.com, judged against the baseline this repo committed.

    The page is the one bot detectors were demonstrated on, so it is the closest
    thing to an outside opinion we can get without a paid detector. It is a 2013
    static page on a volunteer's server, so not reaching it is a skip: the value
    here is the comparison against our own numbers, not whether that host is up
    tonight. Warn-level differences are allowed -- the page does make Firefox
    report window.chrome, which is worth knowing -- but nothing that a detector
    would call an outright automated browser.
    """
    require_camoufox()
    baseline_path = os.path.join(os.path.dirname(__file__), "baselines", "camoufox.json")
    if not os.path.isfile(baseline_path):
        pytest.skip("no committed baseline to compare against")
    baseline = next(iter(load_reports(baseline_path).values()), None)
    if baseline is None:
        pytest.skip("the committed baseline is empty")

    try:
        reports = diagnose(["camoufox"], url=BOT_TEST_PAGE, timeout=120)
    except Exception as exc:
        pytest.skip(f"could not reach {BOT_TEST_PAGE}: {str(exc).splitlines()[0]}")

    found = reports[0]
    if not found.signals.get("userAgent"):
        pytest.skip(f"the probe read nothing from {BOT_TEST_PAGE}")
    hard = {leak.code for leak in found.hard_leaks}
    before = {leak.code for leak in baseline.hard_leaks}
    assert not (hard - before), f"new hard leaks on the test page: {sorted(hard - before)}"
    assert "webdriver_flag" not in hard and "headless_user_agent" not in hard


@live
def test_camoufox_is_reported_to_the_user():
    assert camoufox_version()
