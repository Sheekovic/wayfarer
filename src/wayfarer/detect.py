"""Checking what a browser actually claims, instead of trusting that it does.

A stealth layer is a claim: *this browser reports a consistent human machine*.
Camoufox's own history is the argument for measuring rather than trusting --
after a maintenance gap its stealth quietly degraded, and nothing in a wrapper
would have noticed. So every check here reads the values back out of the live
page and compares them to each other, and the result is saved as a baseline so
a regression shows up as a failing test instead of as a mysterious 403.

The checks are deliberately engine-agnostic JavaScript, so the same report can
be made about vanilla Playwright, Camoufox, or a site's own verdict.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .personas import Persona

#: Signals a page can read about the machine it is running on.
SIGNALS_JS = """
() => {
  const probe = (fn, fallback) => { try { return fn(); } catch (e) { return fallback; } };
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  return {
    userAgent: navigator.userAgent,
    appVersion: probe(() => navigator.appVersion, ''),
    platform: probe(() => navigator.platform, ''),
    webdriver: navigator.webdriver === true,
    languages: Array.from(navigator.languages || []),
    hardwareConcurrency: probe(() => navigator.hardwareConcurrency, null),
    deviceMemory: probe(() => navigator.deviceMemory === undefined ? null : navigator.deviceMemory, null),
    maxTouchPoints: probe(() => navigator.maxTouchPoints, null),
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth],
    window: [window.innerWidth, window.innerHeight, window.outerWidth, window.outerHeight],
    devicePixelRatio: window.devicePixelRatio,
    timezone: probe(() => Intl.DateTimeFormat().resolvedOptions().timeZone, ''),
    doNotTrack: probe(() => navigator.doNotTrack, null),
    plugins: probe(() => navigator.plugins.length, 0),
    mimeTypes: probe(() => navigator.mimeTypes.length, 0),
    pdfViewerEnabled: probe(() => navigator.pdfViewerEnabled, null),
    hasChromeObject: typeof window.chrome !== 'undefined',
    webglVendor: probe(() => ext ? String(gl.getParameter(ext.UNMASKED_VENDOR_WEBGL)) : '', ''),
    webglRenderer: probe(() => ext ? String(gl.getParameter(ext.UNMASKED_RENDERER_WEBGL)) : '', ''),
    automationGlobals: Object.keys(window).filter(
      k => /__playwright|playwright|__cdc|cdc_|domAutomation|webdriver/i.test(k)),
    jugglerGlobals: [
      probe(() => typeof window.QueryContext, 'undefined'),
      probe(() => typeof window.__bindFunctionToJSWindowGlobal, 'undefined'),
    ].filter(t => t !== 'undefined'),
    headlessUa: /Headless|wsl/i.test(navigator.userAgent),
  };
}
"""

#: A canvas, an audio mix and the GPU's parameter list: the three draws a site
#: can compare across visits, so a persona that changes them between loads is
#: announcing that something underneath it is being rewritten.
DRAWS_JS = """
() => {
  const canvas = document.createElement('canvas');
  canvas.width = 240; canvas.height = 60;
  const ctx = canvas.getContext('2d');
  ctx.textBaseline = 'top'; ctx.font = '16px Arial';
  ctx.fillStyle = '#f60'; ctx.fillRect(20, 10, 80, 30);
  ctx.fillStyle = '#002'; ctx.fillText('wayfarer persona', 12, 18);
  const gl = document.createElement('canvas').getContext('webgl');
  let params = '';
  if (gl) {
    for (const name of ['MAX_TEXTURE_SIZE', 'MAX_VERTEX_ATTRIBS', 'MAX_COMBINED_TEXTURE_IMAGE_UNITS',
                        'RED_BITS', 'GREEN_BITS', 'BLUE_BITS', 'ALPHA_BITS', 'DEPTH_BITS', 'STENCIL_BITS',
                        'MAX_TEXTURE_MAX_ANISOTROPY_EXT']) {
      let value = null;
      try { value = gl.getParameter(gl[name]); } catch (e) { value = 'n/a'; }
      params += name + '=' + value + ';';
    }
  }
  let audio = '';
  try {
    const Offline = window.OfflineAudioContext || window.webkitOfflineAudioContext;
    if (Offline) {
      const context = new Offline(1, 1024, 44100);
      const oscillator = context.createOscillator();
      const analyser = context.createAnalyser();
      oscillator.connect(analyser); analyser.connect(context.destination);
      oscillator.start(0); oscillator.stop(0.01);
      const buffer = context.startOffline ? context.startRendering() : null;
      audio = String(context.sampleRate) + ':' + (context.length || '') + ':' + (buffer ? 'rendered' : '');
    }
  } catch (e) { audio = 'error'; }
  return { canvas: canvas.toDataURL(), webglParams: params, audio: audio };
}
"""

_REALISTIC_DPR = (0.75, 1, 1.25, 1.5, 1.6, 1.75, 2, 2.5, 3, 3.5)
_OS_TOKENS = {
    "windows": ("Windows NT", "Win32", "Win64"),
    "macos": ("Macintosh", "Mac OS X", "MacIntel"),
    "linux": ("Linux", "X11"),
}


@dataclass(frozen=True)
class Leak:
    """One thing a page can use to doubt the browser."""

    code: str
    message: str
    #: "leak" is a claim that contradicts itself; "warn" is worth a look.
    severity: str = "leak"

    def to_dict(self) -> Dict[str, str]:
        return {"code": self.code, "message": self.message, "severity": self.severity}


@dataclass
class Report:
    """What one browser told one page about itself."""

    engine: str
    url: str = "about:blank"
    signals: Dict[str, Any] = field(default_factory=dict)
    leaks: List[Leak] = field(default_factory=list)

    @property
    def hard_leaks(self) -> List[Leak]:
        return [leak for leak in self.leaks if leak.severity == "leak"]

    @property
    def codes(self) -> List[str]:
        return sorted(leak.code for leak in self.leaks)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "engine": self.engine,
            "url": self.url,
            "signals": self.signals,
            "leaks": [leak.to_dict() for leak in self.leaks],
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Report":
        return cls(
            engine=str(data.get("engine") or ""),
            url=str(data.get("url") or ""),
            signals=dict(data.get("signals") or {}),
            leaks=[
                Leak(code=str(item.get("code", "")), message=str(item.get("message", "")),
                     severity=str(item.get("severity", "leak")))
                for item in (data.get("leaks") or [])
            ],
        )

    def compare(self, baseline: "Report") -> Dict[str, List[str]]:
        """What this report leaks that ``baseline`` did not, and what it fixed."""
        before, after = set(baseline.codes), set(self.codes)
        return {"new": sorted(after - before), "gone": sorted(before - after)}


def collect_signals(page: Any) -> Dict[str, Any]:
    """Run the probe in a live page. Empty dict when the page moved or closed."""
    try:
        return dict(page.evaluate(SIGNALS_JS) or {})
    except Exception:
        return {}


def fingerprint_draws(page: Any) -> Dict[str, str]:
    """Hash the canvas, GPU and audio draws, so two loads can be compared.

    Measured on Camoufox 0.5.9 across two launches of one saved persona: the
    audio and WebGL-parameter draws are identical, the canvas draw is not --
    Camoufox re-randomises canvas per launch rather than seeding it from the
    pinned identity. So these hashes carry continuity within a session, and the
    canvas one is a per-launch value, not a per-persona one.
    """
    try:
        return _hash_draws(page.evaluate(DRAWS_JS))
    except Exception:
        return {}


async def collect_signals_async(page: Any) -> Dict[str, Any]:
    """Awaitable twin of :func:`collect_signals`, for a page from an async session.

    Handing the sync probe an async page is quietly wrong rather than loudly
    wrong: ``evaluate`` returns a coroutine, the JavaScript never runs, and every
    measurement comes back blank -- which looks like a clean browser.
    """
    try:
        value = page.evaluate(SIGNALS_JS)
        if inspect.isawaitable(value):
            value = await value
        return dict(value or {})
    except Exception:
        return {}


async def fingerprint_draws_async(page: Any) -> Dict[str, str]:
    """Awaitable twin of :func:`fingerprint_draws`."""
    try:
        draws = page.evaluate(DRAWS_JS)
        if inspect.isawaitable(draws):
            draws = await draws
    except Exception:
        return {}
    return _hash_draws(draws)


def _hash_draws(draws: Any) -> Dict[str, str]:
    return {
        key: hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]
        for key, value in dict(draws or {}).items()
    }


def _ua_os(signals: Dict[str, Any]) -> str:
    text = f"{signals.get('userAgent', '')} {signals.get('platform', '')}"
    for os_name, tokens in _OS_TOKENS.items():
        if any(token in text for token in tokens):
            return os_name
    return "unknown"


def lint(signals: Dict[str, Any], persona: Optional[Persona] = None) -> List[Leak]:
    """Compare the claims a page can read against each other.

    Each leak has a stable ``code`` so results can be saved as a baseline and a
    regression shows up as one new code, not as a vague drop in a score.
    """
    if not signals:
        return [Leak("no_signals", "the page reported nothing; the probe did not run")]

    leaks: List[Leak] = []
    add = leaks.append
    renderer = str(signals.get("webglRenderer") or "")
    ua = str(signals.get("userAgent") or "")

    if signals.get("webdriver"):
        add(Leak("webdriver_flag", "navigator.webdriver is true; automation is stated outright"))
    if signals.get("headlessUa"):
        add(Leak("headless_user_agent", f"the user agent names a headless browser: {ua[:80]}"))
    if ", or similar" in renderer:
        add(Leak("webgl_vague_gpu",
                 f"the GPU string hedges, which no real driver writes: {renderer[:90]}"))
    elif not renderer:
        add(Leak("webgl_missing", "no WebGL renderer is reported, which is rare on a desktop browser",
                 "warn"))
    if signals.get("automationGlobals"):
        add(Leak("automation_global",
                 "window globals a bot check looks for: "
                 + ", ".join(sorted(set(signals["automationGlobals"]))[:6])))
    if signals.get("jugglerGlobals"):
        add(Leak("driver_global", "the automation driver left globals behind: "
                 + ", ".join(signals["jugglerGlobals"])))

    screen = list(signals.get("screen") or [])
    window = list(signals.get("window") or [])
    if len(screen) >= 2 and len(window) >= 2 and screen[0] and window[0] > screen[0]:
        add(Leak("window_wider_than_screen",
                 f"a {window[0]}px window cannot exist on a {screen[0]}px screen"))
    ratio = signals.get("devicePixelRatio")
    if ratio is not None and float(ratio) not in _REALISTIC_DPR:
        add(Leak("implausible_pixel_ratio", f"devicePixelRatio {ratio} is not a ratio real displays report",
                 "warn"))

    if not signals.get("timezone"):
        add(Leak("timezone_missing", "no timezone resolved; a machine with no zone is a machine with no profile",
                 "warn"))

    if signals.get("hasChromeObject") and "Firefox" in ua:
        add(Leak("firefox_chrome_object", "Firefox reports window.chrome, which real Firefox does not",
                 "warn"))

    os_name = _ua_os(signals)
    if os_name == "unknown":
        add(Leak("unrecognised_platform", f"the user agent matches no desktop OS: {ua[:80]}", "warn"))
    if persona is not None:
        if persona.os_name and os_name != "unknown" and os_name != persona.os_name:
            add(Leak("os_mismatch",
                     f"the persona is {persona.os_name} but the user agent says {os_name}"))
        if persona.timezone and signals.get("timezone") and signals["timezone"] != persona.timezone:
            add(Leak("timezone_mismatch",
                     f"the persona promises {persona.timezone}, the browser reports {signals['timezone']}"))
        claimed = persona.claimed_ua
        if claimed and ua and claimed != ua:
            add(Leak("ua_mismatch",
                     f"the persona claims {claimed[:60]}, the browser sends {ua[:60]}", "warn"))
        if persona.languages and signals.get("languages"):
            first = str(signals["languages"][0] or "")
            if first and first.split("-")[0] != persona.languages[0].split("-")[0]:
                add(Leak("language_mismatch",
                         f"the persona asks for {persona.languages[0]}, the browser speaks {first}"))
    return leaks


def report(engine: str, signals: Dict[str, Any], persona: Optional[Persona] = None,
           url: str = "about:blank") -> Report:
    """Bundle signals and their verdict into one savable report."""
    return Report(engine=engine, url=url, signals=signals, leaks=lint(signals, persona))


def save_reports(reports: Sequence[Report], path: str) -> None:
    """Write the reports as JSON, creating the directory if it has no reason not to exist.

    ``python -m wayfarer diagnose --json tests/baselines/camoufox.json`` is the
    documented way to pin a baseline, and a first-time clone has no such folder.
    """
    import os

    parent = os.path.dirname(str(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    payload = {"reports": [report.to_dict() for report in reports]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)


def load_reports(path: str) -> Dict[str, Report]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    return {
        str(item.get("engine")): Report.from_dict(item)
        for item in (payload.get("reports") or [])
    }


def diagnose(backends: Sequence[str] = ("playwright", "camoufox"), *, url: str = "about:blank",
             headless: bool = True, persona: Optional[Persona] = None,
             timeout: float = 60.0) -> List[Report]:
    """Launch each backend, load ``url``, and report what each one looks like."""
    from .session import Session  # here, so importing wayfarer stays cheap

    reports: List[Report] = []
    for backend in backends:
        stealth = backend in ("camoufox", "stealth")
        session = Session("firefox" if stealth else "chrome", stealth=stealth,
                          persona=persona if stealth else None, on_challenge="ignore")
        try:
            session.render(url if url != "about:blank" else "about:blank",
                           wait_until="domcontentloaded", timeout=timeout, headless=headless)
            signals = session.signals
            reports.append(report(backend, signals, persona, url))
        finally:
            session.close()
    return reports
