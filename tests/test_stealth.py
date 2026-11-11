"""Tests for the stealth layer: identity, disguise, verification, solving.

None of these need a browser, a network, or a CAPTCHA-solver account: the
browser-facing parts are given fake pages, and the solver parts are given a
fake transport. The live checks -- Camoufox actually launching, the WebGL
string actually landing -- are in tests/test_live.py and skipped by default.
"""

from types import SimpleNamespace

import pytest

from wayfarer import Session, SolverError
from wayfarer.challenges import (
    challenge_kind,
    detect_challenge,
    extract_sitekey,
    looks_challenged,
)
from wayfarer.detect import Report, fingerprint_draws, lint, report
from wayfarer.headers import FETCH, build_headers
from wayfarer.personas import Persona, open_persona, resolve
from wayfarer.profiles import get_profile
from wayfarer.solvers import CapMonster, Solved, SolverChain, apply_token, proxy_fields, solver_from_env
from wayfarer.stealth import clean_renderer, launch_kwargs, patch_webgl

LANGS = ("en-US", "en")


# -- personas ----------------------------------------------------------------

def test_persona_round_trips_through_json(tmp_path):
    persona = Persona.new(seed=7, os_name="macos", languages=("de-DE", "de"))
    persona.preset = {"navigator": {"userAgent": "Mozilla/5.0 (Macintosh; rv:128.0) Firefox/128.0"},
                      "screen": {"width": 1440, "height": 900},
                      "webgl": {"unmaskedVendor": "Apple", "unmaskedRenderer": "Apple M1, or similar"}}
    persona.timezone = "Europe/Berlin"
    persona.touch("example.com", "challenged", "Cloudflare")
    persona.note("synthetic identity")
    path = tmp_path / "me.json"
    persona.save(path)

    back = Persona.load(path)
    assert back.seed == 7 and back.os_name == "macos"
    assert back.languages == ("de-DE", "de") and back.timezone == "Europe/Berlin"
    assert back.screen == (1440, 900)
    assert back.webgl == ("Apple", "Apple M1, or similar")
    assert back.trust_for("example.com") == "challenged"
    assert back.notes == ("synthetic identity",)


def test_persona_rejects_unknown_os():
    with pytest.raises(ValueError):
        Persona.new(os_name="haiku")
    with pytest.raises(ValueError):
        Persona.new(languages=())


def test_missing_persona_file_is_a_first_visit(tmp_path):
    assert open_persona(tmp_path / "nobody.json") is None
    assert open_persona(None) is None


def test_preset_supplies_the_claims():
    persona = Persona.new()
    assert persona.claimed_ua == "" and persona.screen is None
    persona.preset = {"navigator": {"userAgent": "UA-A"}, "screen": {"width": 1, "height": 2}}
    assert persona.claimed_ua == "UA-A" and persona.screen == (1, 2)
    resolve(persona, browser_version="158.0.1-beta.41", ua="UA-B", timezone="Africa/Cairo")
    assert persona.ff_version == 158 and persona.ua == "UA-B"
    assert persona.claimed_ua == "UA-B"     # a measured UA outranks the claim


def test_persona_trust_bookkeeping():
    persona = Persona.new()
    assert persona.trust_for("nope.test") == "unknown"
    entry = persona.touch("a.test", "granted", "Cloudflare")
    persona.touch("a.test")
    assert entry.visits == 2 and entry.trust == "granted" and entry.vendor == "Cloudflare"


# -- the disguise ------------------------------------------------------------

def test_webgl_hedge_is_rewritten_like_a_real_driver():
    assert clean_renderer("ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0), or similar") == \
        "ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0, D3D11)"
    # idempotent, and macOS records have no ANGLE wrapper to rewrite
    once = clean_renderer("Apple M1, or similar")
    assert once == "Apple M1" and clean_renderer(once) == once
    assert clean_renderer("") == ""


def test_launch_kwargs_carry_the_identity_not_the_host():
    persona = Persona.new(seed=3, os_name="windows", languages=("fr-FR", "fr"))
    persona.preset = {"navigator": {"userAgent": "x"}}
    options = launch_kwargs(persona, headless=True, proxy="http://user:p%40ss@host.test:8000")
    assert options["os"] == "windows" and options["headless"] is True
    assert options["locale"] == ["fr-FR", "fr"]          # both languages, in order
    assert options["fingerprint_preset"] == persona.preset   # the same face every launch
    assert options["proxy"] == {"server": "http://host.test:8000",
                                "username": "user", "password": "p@ss"}


@pytest.mark.parametrize(
    "claims,proxy,expect",
    [
        ({"timezone": "Europe/Berlin", "exit_ip": "1.2.3.4"}, None, "berlin"),
        ({"exit_ip": "1.2.3.4"}, "http://host.test:1080", "cached-ip"),
        ({}, "http://host.test:1080", "lookup"),
        ({}, None, "none"),
    ],
)
def test_timezone_comes_from_what_is_known(claims, proxy, expect):
    """Pinned zone first, then the cached exit IP, then one lookup through the proxy."""
    persona = Persona.new()
    for name, value in claims.items():
        setattr(persona, name, value)
    options = launch_kwargs(persona, proxy=proxy)
    if expect == "berlin":
        assert options["config"]["timezone"] == "Europe/Berlin" and "geoip" not in options
    elif expect == "cached-ip":
        assert options["geoip"] == "1.2.3.4" and "config" not in options
    elif expect == "lookup":
        assert options["geoip"] is True
    else:
        assert "geoip" not in options and "config" not in options


def test_webgl_patch_rewrites_and_rechunks_the_payload():
    payload = '{"webGl:vendor":"Google Inc. (Intel)","webGl:renderer":"ANGLE (Intel, Intel(R) HD Graphics 400 ' \
              'Direct3D11 vs_5_0 ps_5_0), or similar","screen.width":2560}'
    size = 64
    env = {f"CAMOU_CONFIG_{i + 1}": payload[i:i + size] for i in range(0, len(payload), size)}
    assert len(env) > 1
    options, applied = patch_webgl({"env": env})
    joined = "".join(options["env"][k] for k in sorted(env, key=lambda k: int(k.split("_")[-1])))
    assert applied and ", or similar" not in joined and ", D3D11)" in joined
    assert '"screen.width":2560' in joined        # everything else survives


def test_webgl_patch_declines_rather_than_corrupting():
    options, applied = patch_webgl({"env": {"CAMOU_CONFIG_1": "not json"}})
    assert applied is False and options["env"]["CAMOU_CONFIG_1"] == "not json"
    assert patch_webgl({})[1] is False


# -- verification ------------------------------------------------------------

CLEAN = {
    "userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
    "platform": "Win32", "webdriver": False, "headlessUa": False, "languages": ["en-US", "en"],
    "screen": [1920, 1080, 1920, 1040, 24], "window": [1280, 720, 1280, 720],
    "devicePixelRatio": 1, "timezone": "America/Chicago", "plugins": 5, "mimeTypes": 2,
    "hasChromeObject": False, "webglVendor": "Google Inc. (Intel)",
    "webglRenderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 Direct3D11 vs_5_0 ps_5_0, D3D11)",
    "automationGlobals": [], "jugglerGlobals": [], "hardwareConcurrency": 8, "deviceMemory": None,
}


def codes(signals, persona=None):
    return {leak.code for leak in lint(signals, persona)}


def test_a_consistent_report_leaks_nothing():
    assert codes(CLEAN) == set()


def test_the_signals_a_detector_uses_are_all_called_out():
    cases = {
        "webdriver_flag": {"webdriver": True},
        "headless_user_agent": {"headlessUa": True},
        "webgl_vague_gpu": {"webglRenderer": "ANGLE (Intel, Intel(R) HD Graphics 400 "
                                               "Direct3D11 vs_5_0 ps_5_0), or similar"},
        "webgl_missing": {"webglRenderer": ""},
        "automation_global": {"automationGlobals": ["__playwright_binding"]},
        "driver_global": {"jugglerGlobals": ["function"]},
        "window_wider_than_screen": {"window": [3840, 720, 3840, 720]},
        "implausible_pixel_ratio": {"devicePixelRatio": 1.37},
        "timezone_missing": {"timezone": ""},
        "unrecognised_platform": {"userAgent": "Mozilla/5.0 (SerenityOS)", "platform": "Serenity"},
    }
    for code, change in cases.items():
        signals = dict(CLEAN, **change)
        assert code in codes(signals), f"{code} not reported for {change}"


def test_a_persona_is_held_to_its_own_claims():
    persona = Persona.new(os_name="macos", languages=("ar-EG", "ar"))
    persona.preset = {"navigator": {"userAgent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7; "
                                                 "rv:128.0) Gecko/20100101 Firefox/128.0"}}
    found = lint(CLEAN, persona)
    assert {leak.code for leak in found} >= {"os_mismatch", "language_mismatch"}
    # A browser reporting Windows under a macOS persona is the classic tell.
    assert all(leak.code != "ua_mismatch" for leak in found) is False
    assert lint(CLEAN, Persona.new()) == []


def test_reports_compare_so_a_regression_is_one_code():
    before = report("camoufox", CLEAN)
    after = report("camoufox", dict(CLEAN, webdriver=True))
    delta = after.compare(before)
    assert delta["new"] == ["webdriver_flag"] and delta["gone"] == []
    back = Report.from_dict(after.to_dict())
    assert back.codes == after.codes


class FakeDrawPage:
    def __init__(self, draws):
        self.draws = draws

    def evaluate(self, script):
        return self.draws


def test_draws_are_hashed_so_they_can_be_compared_across_loads():
    page = FakeDrawPage({"canvas": "data:image/png;base64,AAA", "webglParams": "MAX=16384;",
                         "audio": "44100:1024:rendered"})
    first = fingerprint_draws(page)
    assert set(first) == {"canvas", "webglParams", "audio"}
    assert first["canvas"] != "data:image/png;base64,AAA"
    page.draws = dict(page.draws, canvas="data:image/png;base64,BBB")
    assert fingerprint_draws(page)["canvas"] != first["canvas"]


# -- knowing what was served -------------------------------------------------

def test_sitekeys_are_read_from_the_markup():
    assert extract_sitekey('<div class="cf-turnstile" data-sitekey="0x4AAAAABc"></div>') == "0x4AAAAABc"
    assert extract_sitekey('<div data-sitekey="0x4AAAAABc" class="cf-turnstile"></div>') == "0x4AAAAABc"
    assert extract_sitekey("grecaptcha.render('el', {sitekey: '6LcD3PIbAAAAAJ'})") == "6LcD3PIbAAAAAJ"
    assert extract_sitekey('<div class="h-captcha" data-sitekey="10000000-ffff-ffff-ffff-000000000001">') \
        == "10000000-ffff-ffff-ffff-000000000001"
    assert extract_sitekey("<p>nothing here</p>") is None


def test_the_widget_says_what_to_ask_the_solver_for():
    assert challenge_kind('<div class="cf-turnstile"></div>') == "turnstile"
    assert challenge_kind("src='https://www.google.com/recaptcha/api.js'") == "recaptcha_v2"
    assert challenge_kind('<div class="h-captcha"></div>') == "hcaptcha"
    assert challenge_kind("please enable javascript") is None


def test_a_solved_page_is_judged_on_its_body():
    assert looks_challenged("<title>Just a moment...</title>")
    assert looks_challenged('<iframe src="https://challenges.cloudflare.com/turnstile/v0/api.js">')
    assert not looks_challenged("<h1>the actual article</h1>")


def test_a_block_is_not_marked_solvable():
    blocked = SimpleNamespace(status_code=403, headers={"server": "cloudflare"},
                              content=b"<title>Attention Required! | Cloudflare</title>")
    challenged = SimpleNamespace(status_code=403, headers={"server": "cloudflare"},
                                 content=b"<script src='/cdn-cgi/challenge-platform/h/b/script'></script>")
    assert detect_challenge(challenged).solvable is True
    assert detect_challenge(blocked).solvable is False


# -- paying for a token ------------------------------------------------------

class FakeTransport:
    """Stands in for the solver API: records what was sent, answers on cue."""

    def __init__(self, results):
        self.results = list(results)
        self.sent = []

    def __call__(self, url, headers, body):
        self.sent.append((url, body))
        return self.results.pop(0)


def turnstile_ready():
    return [{"errorId": 0, "taskId": 55},
            {"errorId": 0, "status": "processing"},
            {"errorId": 0, "status": "ready", "solution": {"token": "T1"}}]


def test_capmonster_task_shape_and_polling():
    transport = FakeTransport(turnstile_ready())
    solver = CapMonster("k", transport=transport, poll=0)
    solved = solver.solve("turnstile", sitekey="SK", url="https://x.test/")
    assert isinstance(solved, Solved) and solved.token == "T1" and solved.seconds >= 0
    assert solved.usd > 0 and solved.provider.startswith("CapMonster")
    create_url, create_body = transport.sent[0]
    assert create_url.endswith("/createTask")
    assert create_body["clientKey"] == "k"
    assert create_body["task"]["type"] == solver.task_types["turnstile"]
    assert create_body["task"]["websiteKey"] == "SK"
    assert "proxyType" not in create_body["task"]          # proxyless task
    assert transport.sent[-1][0].endswith("/getTaskResult")


def test_cf_clearance_carries_the_proxy_and_the_page():
    transport = FakeTransport([
        {"errorId": 0, "taskId": 7},
        {"errorId": 0, "status": "ready", "solution": {"cf_clearance": "CLEAR"}},
    ])
    solved = CapMonster("k", transport=transport, poll=0).solve(
        "cf_clearance", sitekey="SK", url="https://x.test/",
        proxy="http://u:p@1.2.3.4:8080", user_agent="UA", page_html="<html>blocked</html>")
    task = transport.sent[0][1]["task"]
    assert task["proxyAddress"] == "1.2.3.4" and task["proxyLogin"] == "u"
    assert task["proxyPassword"] == "p" and task["proxyPort"] == "8080"
    assert task["userAgent"] == "UA" and "<html>blocked</html>" not in task["htmlPageBase64"]
    assert solved.cookies == {"cf_clearance": "CLEAR"}

    with pytest.raises(SolverError):     # the clearance is bound to an exit address
        CapMonster("k", transport=FakeTransport(turnstile_ready()), poll=0).solve(
            "cf_clearance", sitekey="SK", url="https://x.test/")


def test_solver_errors_are_named_not_swallowed():
    transport = FakeTransport([{"errorId": 1, "errorDescription": "ERROR_ZERO_BALANCE"}])
    with pytest.raises(SolverError) as info:
        CapMonster("k", transport=transport, poll=0).solve("turnstile", sitekey="S", url="https://x.test/")
    assert info.value.code == "ERROR_ZERO_BALANCE"


def test_a_chain_moves_on_when_the_first_solver_gives_up():
    first = CapMonster("k", transport=FakeTransport([{"errorId": 1, "errorDescription": "ERROR_CAPTCHA_UNSOLVABLE"}]),
                       poll=0)
    second = CapMonster("k", transport=FakeTransport(turnstile_ready()), poll=0)
    solved = SolverChain([first, second]).solve("turnstile", sitekey="S", url="https://x.test/")
    assert solved.token == "T1"


def test_no_keys_means_no_solver(monkeypatch):
    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)
    assert solver_from_env() is None
    with pytest.raises(SolverError):
        CapMonster()                       # no key given, none in the environment


def test_proxy_fields_keep_credentials_and_socks_names():
    assert proxy_fields("socks5h://h.test:1080") == {"proxyType": "socks5", "proxyAddress": "h.test",
                                                     "proxyPort": "1080"}
    assert proxy_fields(None) == {}
    with pytest.raises(ValueError):
        proxy_fields("not a url")


class FakePage:
    def __init__(self):
        self.evaluated = []
        self.clicked = []

    def evaluate(self, script, arg):
        self.evaluated.append(arg)
        return True

    def click(self, selector, timeout=0):
        self.clicked.append(selector)


def test_a_token_goes_to_the_field_the_widget_wrote():
    page = FakePage()
    assert apply_token(page, "turnstile", "T1") is True
    assert page.evaluated[0] == {"field": "cf-turnstile-response", "token": "T1"}
    assert page.clicked                                      # the form gets submitted
    assert apply_token(page, "cf_clearance", "cookie") is False   # a cookie, not a field
    assert apply_token(page, "hcaptcha", "") is False


# -- the ladder --------------------------------------------------------------

def test_the_priority_header_is_computed_even_if_curl_cffi_owns_sending_it():
    headers = build_headers(get_profile("chrome"), mode=FETCH, method="GET", url="https://x.test/api",
                            page_url="https://x.test/", languages=LANGS)
    assert headers["Priority"] == "u=1, i"


def test_on_challenge_rejects_invented_modes():
    with pytest.raises(ValueError):
        Session(on_challenge="shrug")


def test_stealth_persona_file_is_written_on_close_without_launching_a_browser(tmp_path):
    path = tmp_path / "me.json"
    with Session("firefox", stealth=True, persona=path, delay=0) as s:
        assert s.stealth and s.persona is not None
        assert s.identity["engine"] == "camoufox" and s.identity["measured"] is False
    assert path.exists()
    assert Persona.load(path).languages == ("en-US", "en")


class FakeSolver:
    """Stands in for a CAPTCHA service, recording what the page was worth."""

    def __init__(self, token="T1"):
        self.token = token
        self.asks = []

    def solve(self, kind, **kwargs):
        self.asks.append(dict(kwargs, kind=kind))
        return Solved(kind=kind, token=self.token, provider="FakeSolver", seconds=1.5, usd=0.001)


class LadderPage:
    """A page that can be shown a token, and then reports what it looks like."""

    def __init__(self, url, html, cleared_html):
        self.url = url
        self.html = html
        self.cleared_html = cleared_html
        self.clicked = []

    def evaluate(self, script, arg=None):
        self.html = self.cleared_html           # the token landed
        return True

    def click(self, selector, timeout=0):
        self.clicked.append(selector)

    def wait_for_load_state(self, state, timeout=0):
        return None

    def content(self):
        return self.html


def snapshot(url, status, html, headers=None):
    """A stand-in for a rendered page; the headers are what a goto response gave."""
    return SimpleNamespace(url=url, status_code=status,
                           headers=headers if headers is not None else {"server": "cloudflare"},
                           html=html, content=html.encode(), title="t")


def test_the_ladder_pays_a_solver_then_replays_the_request(site, monkeypatch):
    """Challenge -> solve in the browser -> the plain path replays and gets through."""
    solver = FakeSolver()
    hook_results = []

    def fake_render(url, **kwargs):
        page = LadderPage(url, '<div class="cf-turnstile" data-sitekey="0x4AAAAAAACEjISTEalY4zYF"></div>',
                          "<h1>the article</h1>")
        hook = kwargs.get("on_page")
        if hook is not None:
            hook_results.append(hook(page, snapshot(url, 403, page.html)))
        return snapshot(url, 200, "<h1>the article</h1>")

    with Session(on_challenge="solve", solver=solver, **dict(delay=0)) as s:
        monkeypatch.setattr(s, "render", fake_render)
        response = s.visit(site.url + "/cf-clears")

    assert response.status_code == 200 and "served" in response.text
    assert hook_results == [True]
    assert solver.asks[0]["kind"] == "turnstile"
    assert solver.asks[0]["sitekey"] == "0x4AAAAAAACEjISTEalY4zYF"
    assert len(site.hits("/cf-clears")) == 2          # challenged once, then replayed


def test_the_ladder_gives_up_once_per_host_with_a_trail(site, monkeypatch):
    from wayfarer import ChallengeError

    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)
    monkeypatch.delenv("TWOCAPTCHA_API_KEY", raising=False)

    def fake_render(url, **kwargs):
        page = LadderPage(url, "<title>Just a moment...</title>", "<title>Just a moment...</title>")
        hook = kwargs.get("on_page")
        if hook is not None:
            hook(page, snapshot(url, 403, page.html))
        return snapshot(url, 403, page.html)

    with Session(on_challenge="solve", **dict(delay=0)) as s:
        monkeypatch.setattr(s, "render", fake_render)
        with pytest.raises(ChallengeError) as info:
            s.visit(site.url + "/cf")
        trail = " ".join(info.value.trail)
        assert "browser" in trail and "CAPMONSTER_API_KEY" in trail
        with pytest.raises(ChallengeError) as again:
            s.visit(site.url + "/cf")
        assert "already walked" in " ".join(again.value.trail)
    assert len(site.hits("/cf")) == 2                 # bounded: it does not loop


def test_an_unsolved_challenge_marks_the_persona(site, monkeypatch):
    monkeypatch.delenv("CAPMONSTER_API_KEY", raising=False)

    def fake_render(url, **kwargs):
        return snapshot(url, 403, "<title>Just a moment...</title>")

    from wayfarer import ChallengeError, Persona

    persona = Persona.new()
    with Session("firefox", stealth=True, persona=persona, on_challenge="solve",
                 **dict(delay=0)) as s:
        monkeypatch.setattr(s, "render", fake_render)
        with pytest.raises(ChallengeError):
            s.visit(site.url + "/cf")
    assert persona.trust_for("127.0.0.1") == "unsolved"
    assert persona.hosts["127.0.0.1"].vendor == "Cloudflare"
