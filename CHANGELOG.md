# Changelog

## 0.2.0

wayfarer could already visit a site the way a browser does. This release is
about the sites that were not convinced.

### Added

- **Stealth rendering.** `Session(..., stealth=True)` renders in Camoufox, where
  the fingerprint is spoofed in the browser engine instead of patched in
  JavaScript. The default is unchanged: `render()` still uses the machine's own
  browser and still admits it is automated.
- **Personas** (`wayfarer.personas.Persona`). A visitor identity — recorded
  device fingerprint, locale, timezone, bound proxy, cookies, and which hosts
  have already accepted or rejected it — saved to one JSON file and reloaded, so
  the next run resumes the same visitor instead of starting anonymous. Camoufox
  draws a new random fingerprint per launch; a persona hands back the one it drew.
- **A challenge ladder.** `on_challenge="solve"` escalates from the plain request
  to a browser load as the same visitor to a CAPTCHA solver, and raises
  `ChallengeError` with a `trail` describing each rung it tried and what it cost.
  The old modes, `"raise"` and `"ignore"`, are unchanged and still the default.
- **Solver clients** (`CapMonster`, `TwoCaptcha`, `SolverChain`,
  `solver_from_env`) speaking the `createTask` / `getTaskResult` API over plain
  HTTP, with `proxy_fields()` and a `balance()` check, and no Node dependency.
  Solving needs a key, not a package.
- **Verification, because stealth claims rot.** `python -m wayfarer diagnose`
  reports what a page could read back about each backend — automation globals,
  timezone and OS coherence against the persona, geometry that cannot exist, a
  GPU string no real driver writes — with stable leak codes so a regression is a
  diff. `wayfarer detect.lint()` and `detect.report()` are the same thing as an
  API, and `Session.signals` / `Session.identity` expose the measurements.
- **`python -m wayfarer doctor`**: Camoufox build present or not, which Firefox
  version `curl_cffi` impersonates (they have to agree for a solved cookie to
  survive), solver keys and balance.
- **GPU-string fix.** Every WebGL renderer Camoufox can emit ends in
  `", or similar"` (10 of 11 Windows records, all macOS ones), a hedge no real
  driver writes. wayfarer rewrites it into the shape Firefox reports inside the
  config payload handed to the browser, then reads the live value back.
- **`Session.render(..., on_page=callback)`** — the callback receives
  `(page, snapshot)` while the page is still open, which is how a challenge gets
  solved in the identity that was served it, and how a caller can drive a page
  without owning a Playwright context.

### Changed

- `challenges.Challenge` gained `solvable`, so a flat block is never mistaken for
  a puzzle a solver could pass; `detect_challenge()` sets it per vendor. New
  helpers `extract_sitekey()`, `challenge_kind()` and `looks_challenged()`.
- `ChallengeError` gained `.trail`. The message now includes what was tried.
- `render()` returns the same `RenderedPage` as before for both backends, and the
  Camoufox host records cookies back into the session exactly as the Playwright
  one did, so `visit()`/`fetch()` keep working as the same visitor.

### Fixed

- Tests no longer assume `curl_cffi` forwards a caller-supplied `Priority`
  header: 0.17 stopped forwarding it for Chrome targets (it emulates stream
  priority itself). Firefox targets still do, and the header wayfarer computes
  is asserted directly in `tests/test_stealth.py`.

### Notes

- Solver request shapes are encoded in one table per provider and documented
  against provider docs; they have not been exercised against a paid account in
  this release, so `solver.balance()` is the first thing to try with a new key.
- Extra installs: `wayfarer[browser]` (Playwright), `wayfarer[stealth]`
  (Camoufox + Playwright). Camoufox fetches ~200 MB of browser on first use;
  wayfarer does not do that download for you.

## 0.1.0

First release: browser TLS and HTTP/2 fingerprints, browser-shaped headers with
per-request `Sec-Fetch-*` context, per-host pacing with jitter, retries that
honour `Retry-After`, cookie persistence, robots.txt, proxy pools, unmodified
`render()` through Playwright, and challenge detection.
