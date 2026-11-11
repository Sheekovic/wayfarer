# wayfarer × Camoufox: the anti-detect plan

Goal: make `render()` a browser that bot detection cannot tell is automated, keep the
plain-HTTP path (`visit()`/`fetch()`) working as the *same visitor* after the browser
earns trust, and get past real CAPTCHAs when the site insists. "Make our own" here means
owning the layer above Camoufox — persona, continuity, verification, escalation — not
forking Firefox itself.

Everything below was measured on this machine today, not assumed.

---

## 1. What today looks like (measured)

Same probe (24 browser signals + the classic bot test page), run twice:

| Signal | `render()` today (real Chrome, headless) | Camoufox 0.5.9, default launch |
| --- | --- | --- |
| `navigator.webdriver` | **true** — instant fail | false |
| User-Agent | `HeadlessChrome/157.0.0.0` — **says headless** | `Firefox/158.0` (no headless marker) |
| Automation globals / Playwright leaks | none visible | none visible |
| bot.sannysoft.com | 57/58 pass — fails on **WebDriver present** | 57/58 pass — fails on **`window.chrome` missing** (a real Firefox also fails that one, so it is a false positive, not a leak) |
| WebGL renderer | real `NVIDIA GeForce RTX 5090` — real hardware, *inconsistent* with the 1280×720 screen | spoofed, but see defect 1 below |
| Timezone | `Africa/Cairo` (host) | `Africa/Cairo` (host — **not** rotated with the persona) |
| Screen / DPR | 1280×720 @ 1 | 3440×1440 @ 1 while the window is 1280×720 |

So: today `render()` is openly automated (the README already admits this). Camoufox fixes
the headline signals for free, and its engine-level spoofing is the right foundation.
But it is not clean out of the box, and a wrapper that just calls `Camoufox()` and prays
is what everyone else already ships. Two defects I hit in a single default launch:

1. **WebGL renderer string is a known artifact.** It came back as
   `ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0), or similar`.
   The `, or similar` tail is literally in Camoufox's `webgl/webgl_data.db`
   (`webGl:renderer: "Apple M1, or similar"`) — the dataset is keyed off *Chrome* reports,
   and no real Firefox driver ever emits that suffix. One lookup on a WebGL string flags it.
2. **Persona and host leak into each other.** Screen size and timezone came from this
   machine, while the GPU is a 2013-era Intel HD 400. Ultrawide monitor + decade-old GPU +
   Cairo timezone is a combination Camoufox generated for itself, and it is the kind of
   cross-signal disagreement the project's own status note admits still hurts it.

And one blocker specific to wayfarer, found by checking the versions:

- Camoufox's build is **Firefox 158.0.1-beta.41**. `curl_cffi` 0.14.0 topped out at
  **firefox144** when this was measured; 0.17.0 reaches **firefox155**, which narrows the
  gap but does not close it. So the browser and the plain-HTTP side of one session
  disagree on the browser version — which matters because Cloudflare binds `cf_clearance`
  to the exact User-Agent (and is sensitive to the TLS/HTTP2 fingerprint behind it).
  Handing a solved `cf_clearance` from the browser to `fetch()` would carry a UA
  mismatch and silently fail. `python -m wayfarer doctor` prints both versions, and the
  solver is asked with the UA the live browser is sending rather than the recorded one.
- `render()` also throws away all browser state after each call (`new_context()` … `close()`),
  so nothing a challenge granted survives the call.

Conclusion: the fingerprint engine is a solved problem we can buy for free; **consistency,
continuity and verification are the parts nobody does well.** That's what we build.

---

## 2. The central idea: one persona, two engines

A **persona** is a single visitor identity that both the browser and the plain-HTTP path
agree on, and that survives process restarts:

```jsonc
// persona.json — one file, one visitor, resumable
{
  "version": 1,
  "seed": 1739284710,
  "browser": { "family": "firefox", "ff_version": 158, "ua": "…Firefox/158.0" },
  "fingerprint": { "os": "win", "screen": [2560, 1440], "dpr": 1.25,
                   "webgl": {"vendor": "Google Inc. (Intel)",
                             "renderer": "ANGLE (Intel, Intel(R) UHD Graphics 630 …)"},
                   "fonts_profile": "win11", "hardware_concurrency": 8 },
  "geo": { "locale": "en-US", "languages": ["en-US", "en"],
           "timezone": "America/Chicago", "exit_ip": "1.2.3.4", "proxy": "http://…:3128" },
  "profile_dir": "personas/1739284710/",
  "hosts": { "example.com": { "visits": 4, "last_seen": "…",
                              "trust": "granted", "cookies": "cf_clearance" } }
}
```

Three rules make this worth having:

1. **Persona ↔ proxy are bound.** A sticky proxy from the existing `ProxyPool` always gets
   the same persona, and the persona's timezone/locale/geolocation are derived from that
   proxy's *exit IP* (Camoufox ships `geoip`/`ip.py` helpers for this). Rotation is not
   "new random fingerprint"; it is "new proxy + the persona that belongs to it".
2. **Persona ↔ browser fingerprint are pinned, not re-rolled.** Camoufox re-rolls a random
   fingerprint on every launch by default. We pass a pinned `fingerprint=` (browserforge is
   already installed: 1.2.3) plus `persistent_context=True` + a stable profile dir, so
   repeat visits present the same identity — a returning visitor, which is exactly what a
   synthetic first-timer cannot fake.
3. **Persona ↔ HTTP identity are reconciled, not assumed.** On first browser launch we read
   `navigator.userAgent` *back out of the live browser* and use it as the session's UA
   override, and pick the closest `curl_cffi` target. Then we verify with an echo endpoint
   (e.g. `tls.peet.ws` for JA3/JA4/ALPN, a UA echo for headers) and refuse to hand
   challenge cookies across engines whose fingerprints disagree — instead of pretending.

That last rule is the one that turns "we can run Camoufox" into "a challenge solved in the
browser keeps `fetch()` unbanned", which is wayfarer's actual selling point.

---

## 3. Module layout (additive; nothing existing moves)

```
src/wayfarer/
  personas.py     NEW  Persona dataclass, JSON load/save, fingerprint pinning,
                       proxy→geo binding, host trust bookkeeping
  stealth.py      NEW  persona + Profile → Camoufox launch options; the WebGL-string
                       fixup; prefs/font/screen-DPR coherence; leak warnings as errors
  backends.py     NEW  BrowserBackend protocol: PlaywrightBackend (today's behaviour,
                       still the default) | CamoufoxBackend | (later) ChromeCDPBackend
  detect.py       NEW  the self-check: signal collector, consistency linter,
                       canvas/audio hash stability, CLI `python -m wayfarer diagnose`
  solvers.py      NEW  Solver protocol + CapMonster, TwoCaptcha (plain HTTP polling,
                       no Node dependency), token/cookie results with cost + latency
  challenges.py   EDIT vendor + kind (+ solvable, sitekey extraction, attempt trail)
  session.py      EDIT on_challenge "solve", stealth=, persona=; keep render() output
  browser.py      EDIT delegate to backends; RenderedPage/NetworkCall API unchanged
  errors.py       EDIT ChallengeError gains .trail (what was tried, what each cost)
```

Public API stays boring — that's deliberate, the current README's tone is an asset:

```python
from wayfarer import ProxyPool, Session

with Session("firefox",
             stealth=True,                     # Camoufox instead of vanilla Playwright
             persona="me.json",                # resume the same visitor next run
             proxy=pool,                       # proxy and persona stay bound
             on_challenge="solve") as s:        # raise | ignore | solve
    page = s.render("https://hard-site.example/", wait_for="#results")
    data = s.fetch("https://hard-site.example/api/items").json()   # same visitor, no browser
    print(s.identity)          # resolved UA / browser / tz / exit IP the two engines agreed on
    print(s.persona.trust_for("hard-site.example"))   # granted / challenged / unsolved
```

Defaults do not change: `stealth=False` and `on_challenge="raise"` keep today's behaviour,
so nothing quietly starts spending money or launching a 200 MB browser.

---

## 4. The escalation ladder

`on_challenge="solve"` walks this, cheapest first, and records every rung in
`ChallengeError.trail` when it gives up:

```
L0  plain HTTP (curl_cffi TLS + header context)         ~0.1 s   free        <- today's default
L1  same identity, wait out the rate limit               s         free        <- Retry-After, pacing already exist
L2  Camoufox render, same persona + same proxy           1-10 s    free        <- JS/managed challenges
L3  solver, same proxy + same UA, cookies handed back    10-40 s   $0.60-2.50/1K
L4  new persona + new proxy, rebuild session, one retry  5-20 s    free
    └─ ChallengeError(vendor, kind, trail)
```

L2 already covers a lot of Cloudflare "managed challenge" and non-interactive Turnstile:
an engine-level spoof plus a real SpiderMonkey and real behaviour often passes without
paying anyone. L3 is where CapMonster/2Captcha come in, and only for what genuinely needs
a token (`cf_clearance`, Turnstile widget, reCAPTCHA v2/v3, hCaptcha, DataDome cookie).

Hard constraints at L3, stated up front because they decide the architecture:

- a `cf_clearance` solve **requires a proxy** and must be submitted with the same
  User-Agent that will replay it → the solver gets the session's sticky proxy, always;
- the returned cookie must be replayed by a client whose TLS/HTTP2 fingerprint is close to
  the one that earned it → we only hand it to the curl side after the echo check passes,
  otherwise we keep using the browser for that host and say so;
- paying to bypass a block the site deliberately served is **opt-in**, so
  `on_challenge="solve"` stays off by default and robots.txt still applies.

---

## 5. Where we can actually be better than what already exists

Ranked by how much detection surface each one removes:

1. **A verification harness, not vibes.** `python -m wayfarer diagnose` prints a machine-
   readable leak report, and the same checks run in CI against saved baselines. Every other
   Camoufox wrapper I found ships a launch function and no way to know if it still works
   — while Camoufox's own 2026 status note concedes detection performance degraded silently
   during its maintenance gap. Regression tests are the whole game on a moving target.
2. **Persona persistence.** Camoufox re-rolls a fingerprint per launch; nobody I saw
   persists identity across runs. This is continuity, which is wayfarer's whole thesis.
3. **Persona ↔ proxy ↔ geo coherence** (timezone/locale/geolocation from the exit IP),
   which kills the class of mismatch my probe already caught on a default launch.
4. **Cross-engine identity reconciliation with proof** (read the UA from the live browser,
   echo-check JA3/JA4/UA agreement before trusting a handoff).
5. **Fix the artifacts we can reach from Python**, then push them upstream: strip
   `, or similar` and re-render the WebGL string in Firefox's ANGLE shape for the claimed
   GPU; make screen/DPR/window-geometry coherent; align font lists with the claimed OS.
6. **Behaviour** — Camoufox's `humanize` plus wayfarer's existing per-host pacing, with a
   dwell/scroll before the first interaction. Cheap, and it is the signal that catches
   fingerprint-perfect bots.
7. **Async + fan-out parity**: one browser per session, plus documented
   `python -m camoufox server --port N` + `firefox.connect(ws)` for parallel personas,
   since `AsyncSession` already exists.

Explicit non-goal: **don't fork Firefox.** Maintaining a Firefox fork (rebase, security
updates, ~200 MB binaries) is a different project than maintaining a Python library, and
our fixes are data/format-level, so they belong upstream as pull requests.

---

## 6. Phases, each independently shippable

**P0 — probe harness (½ day).** Commit the probe I ran today as `detect.py` +
`diagnose` skeleton, save both JSON baselines (vanilla Playwright vs Camoufox).
*Accept:* `python -m wayfarer diagnose --backend playwright,camoufox` prints a leak report;
`tests/baselines/*.json` exist; existing 3 test files still pass untouched.

**P1 — backends + personas (1 day).** `BrowserBackend` split, `CamoufoxBackend`,
pinned fingerprint + persistent profile dir, persona JSON, proxy binding, UA read-back.
*Accept:* `render()`/`RenderedPage`/`api_calls` API unchanged; unit tests for persona
round-trip and the launch-option builder (pure, no browser); one live-gated `slow` test
against bot.sannysoft.com asserting **no new leaks vs baseline**.

**P2 — consistency linter + fixes (1 day).** WebGL string, screen/DPR/geometry, fonts,
timezone↔exit-IP, canvas+audio hash stability (same seed → same hash across reloads;
different seeds → different hashes).
*Accept:* `diagnose` score beats the P0 baseline JSON; the two defects from §1 are gone.

**P3 — solving + handoff (1–2 days, needs a key from you).** CapMonster first, 2Captcha as
fallback; `on_challenge="solve"`; ladder above; `ChallengeError.trail`.
*Accept:* on a site that serves a real challenge, browser render → solve → the *plain-HTTP*
`fetch()` then returns 200 with content, and the failure case reports why (UA mismatch,
proxy rejected, unsolvable) instead of a bare `ChallengeError`.

**P4 — continuity + docs (1 day).** Persona resume across runs, host trust bookkeeping,
`wayfarer doctor` (is the 200 MB binary present, which build, solver balance, echo check),
pyproject extras `wayfarer[stealth]` / `wayfarer[solve]`, and the README rewrite — the
Scope section currently promises "does not solve CAPTCHAs, run JavaScript challenges, or
forge challenge tokens" and must stop saying that.

**P5 — optional, later.** A `ChromeCDPBackend` that attaches to your *real* Chrome profile
(`connectOverCDP`): real cookies, history and reputation beat any synthetic persona, and
it is the honest answer for Chrome-only sites that test V8 quirks Camoufox cannot spoof.

---

## 7. Risks and the honest limits

- **Camoufox is a moving target.** Its own docs report a maintenance gap that degraded
  stealth, and at least one 2026 detector benchmark reports detecting Camoufox. Mitigation
  is P0: pinned binary version, baselines in CI, and `PlaywrightBackend` stays as fallback.
- **Firefox only.** Camoufox spoofs fingerprint values, not the JS engine. Sites that lean
  on Chrome/V8-specific behaviour need the P5 real-Chrome path, not a better Firefox.
- **`cf_clearance` is fragile** (bound to IP + UA, sensitive to TLS). The design assumes
  handoff *may* fail and says so loudly; only P3's live test will tell us how often.
- **Version skew** between Camoufox's Firefox 158 build and `curl_cffi`'s newest Firefox 144
  target. We pin and verify rather than hope; if the gap is fatal for handoff, we cap the
  persona's Firefox version to what both sides can express (`ff_version` exists in Camoufox).
- **Solvers are a paid third party**: 10–40 s latency, roughly $0.60–$2.50 per 1,000,
  and `ERROR_CAPTCHA_UNSOLVABLE` happens. Never silent: cost lands in `ChallengeError.trail`.
- **Install weight**: Camoufox's binary is ~200 MB plus a fetch step; it stays an extra, and
  `doctor` tells the user exactly what is missing. On Windows no virtual display is needed
  (that's the Linux path); on Linux Camoufox's built-in virtual display is a real advantage.
- **Politeness**: solving a CAPTCHA to get past a wall is a different decision from pacing
  and robots.txt, so it stays opt-in and documented as such.

---

## 8. What I need from you

1. A **CapMonster or 2Captcha key** (`CAPMONSTER_API_KEY` / `TWOCAPTCHA_API_KEY` — neither
   is set here) for P3, and ideally **one residential proxy URL**, since `cf_clearance`
   solving requires a proxy.
2. **One real target site** you actually want to visit — it should be the acceptance test
   for P3, not a public demo page.
3. Nod on keeping the defaults as they are (`stealth=False`, `on_challenge="raise"`), so the
   library's current honest behaviour stays honest unless you ask for the heavier tools.

---

## 9. What the finished build measures (P0–P4, same machine)

Not a plan any more — these were read out of live browsers while building, and the checks
are committed as `tests/test_live.py` (run with `WAYFARER_LIVE=1`) plus
`tests/baselines/*.json`.

| Claim | Measurement |
| --- | --- |
| Playwright version matters more than the disguise | Playwright 1.65 (outside Camoufox 0.5.9's declared `<1.63`) fails **every** Camoufox context inside `new_context` with a protocol error. 1.62 works. `doctor` prints the pairing; the `stealth` extra carries the ceiling. |
| The WebGL rewrite lands | `diagnose` reads the live page: `ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0, D3D11)` — no `", or similar"` — across three launches, headless, Windows. |
| The disguise reports no contradictions | `diagnose --backends playwright,camoufox`: playwright **2** hard leaks (`webdriver_flag`, `headless_user_agent`), camoufox **0** leaks, and `same as baseline` against the committed baseline. |
| A persona is the same visitor tomorrow | Two launches from one saved persona file: user agent, WebGL vendor **and** renderer, screen, timezone, `hardwareConcurrency`, platform all identical. Audio and WebGL-parameter draws identical. |
| …with one honest exception | The **canvas** draw differs between launches: Camoufox 0.5.9 re-randomises canvas per launch rather than seeding it from the pinned identity. Documented as a limit, asserted nowhere, because it is upstream's choice. |
| The solver ladder works without a paid account | Turnstile sitekey extraction, task shape, polling, token injection and the give-up trail are covered against a fake transport (35 offline checks). The network half has never seen a real key — `doctor --balance` is the first thing to run with one. |

Two things this section exists to stop being rediscovered: the disguise is only as good as
the *pairing* of its parts (Camoufox ↔ Playwright ↔ `curl_cffi`), and every claim above is a
`lint()` code or a live test, so a browser update that breaks one shows up as a diff rather
than as a slow week of 403s.

Suggested order: P0 and P1 need nothing from you and change nothing for existing users —
I can start there, and the `diagnose` output will tell us how much work P2 really needs.
