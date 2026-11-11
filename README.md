# wayfarer

Visit websites from Python the way a real browser would.

Most sites that turn scripts away do it because the script is obviously not a
browser: the TLS handshake is Python's, the headers are in the wrong order, an
API call claims to be a page navigation, there is no Referer, and requests
arrive in a burst. wayfarer removes those inconsistencies, so an ordinary,
well-behaved visit is treated as one.

When a site still wants proof, ask it twice: `stealth=True` renders in a
browser whose fingerprint is spoofed in the engine rather than in JavaScript,
and `on_challenge="solve"` walks from there up to a CAPTCHA solver — keeping the
same identity, proxy and cookies the whole way.

```python
from wayfarer import Session

with Session("chrome") as s:
    page = s.visit("https://example.com/")            # a page load
    items = s.fetch("https://example.com/api/items")  # what the page's JS would do
    print(page.status_code, items.json())
```

Responses are `curl_cffi` responses (`.status_code`, `.text`, `.json()`, `.headers`, `.url`).

## What it does

| Layer | Behaviour |
| --- | --- |
| Handshake | Real browser TLS and HTTP/2 fingerprints via [curl_cffi](https://github.com/lexiforest/curl_cffi). Profiles: `chrome`, `firefox`, `safari`, `chrome_android`, `safari_ios`. |
| Headers | The profile's own default headers, plus per-request context: `Sec-Fetch-Site/Mode/Dest/User`, `Referer` (strict-origin-when-cross-origin), `Origin`, `Priority`, and `Accept-Language` formatted the way that browser family writes it. |
| Browsing state | The session remembers which page it is on. `visit()` moves to a new page; `fetch()` is a call made from the current page. |
| Pacing | Per-host delay with jitter (default 0.5 to 1.5 s). The first request to a host never waits. |
| Retries | Idempotent requests retry on 429/502/503/504 and network errors with backoff, honouring `Retry-After`. |
| Cookies | Kept for the session; `cookie_file="cookies.json"` saves and restores them between runs. |
| robots.txt | `respect_robots=True` checks rules before each request and applies `Crawl-delay`. |
| Challenges | A bot-challenge page raises `ChallengeError` naming the vendor, instead of being returned as if it were content. With `on_challenge="solve"` wayfarer escalates instead of stopping. |
| Stealth | `stealth=True` renders in [Camoufox](https://github.com/daijro/camoufox), where `navigator.*`, WebGL, canvas and screen are rewritten in C++ rather than patched in JavaScript. |
| Identity | A persona — one fingerprint, timezone, proxy and cookie jar — persists to a file, so tomorrow's run is the same visitor and not a stranger with a new face. |
| Async | `AsyncSession` has the same options and methods. |

## Options

```python
Session(
    "firefox",
    languages=("de-DE", "de"),
    delay=(1, 3),                 # or a number; 0 turns pacing off
    retry=RetryPolicy(attempts=5),
    respect_robots=True,
    cookie_file="cookies.json",
    proxy="http://127.0.0.1:8080",
    timeout=20,
    on_challenge="raise",         # raise | ignore | solve
)
```

`request()`, `get()`, `post()` and friends take `mode="navigate" | "fetch" | "auto"`.
With `auto`, a request with `json=` is a fetch and anything else is a navigation.
Per-request `headers=` override the computed ones case-insensitively; a value of
`None` removes a header.

## Proxies

```python
Session(proxy="http://127.0.0.1:8080")                  # every request
Session(proxy="http://user:password@proxy.example:3128")  # with credentials
Session(proxy="socks5h://127.0.0.1:1080")               # SOCKS, DNS resolved by the proxy
s.visit(url, proxy="http://127.0.0.1:8080")             # one request only
```

HTTP, HTTPS, SOCKS4 and SOCKS5 proxy URLs are accepted (handled by libcurl).
Credentials are sent to the proxy only, never to the site. HTTPS sites are
tunnelled with CONNECT, so the browser TLS fingerprint is unchanged. Pacing,
retries, cookies and robots.txt work the same through a proxy.

### Proxy pools

For a provider that gives one host with a range of ports:

```python
import os
from wayfarer import ProxyPool, Session

pool = ProxyPool.from_range(
    os.environ["PROXY_HOST"], range(10000, 11000),
    username=os.environ["PROXY_USER"], password=os.environ["PROXY_PASS"],
)
with Session(proxy=pool) as s:
    s.visit("https://example.com/")
    print(s.proxy)   # the proxy this session was given
```

A session is one visitor: it takes one proxy from the pool and keeps it, so
its address, cookies and browser profile stay together. It moves to another
proxy only after a network error, as part of the normal retry. `ProxyPool([...])`
also accepts an explicit list of proxy URLs. Keep credentials in environment
variables rather than in source files.

## Pages that need JavaScript

```python
with Session("chrome") as s:
    page = s.render("https://example.com/app", wait_for="#results")
    print(page.title, page.html)
    data = s.fetch("https://example.com/api/items").json()   # same visitor, plain HTTP again
```

`render()` opens the page in a real browser through Playwright
(`pip install 'wayfarer[browser]'`) and returns a `RenderedPage` (`.url`,
`.status_code`, `.html`, `.title`, `.headers`) after the JavaScript has run.
The session's cookies, proxy and language go into the browser, and cookies the
page sets come back, so `visit()` and `fetch()` carry on as the same visitor.
Pacing, robots.txt and challenge detection apply as usual.

Without `stealth`, the browser is the one installed on the machine (Chrome
first, then Playwright's own build; Firefox and Safari profiles use Playwright's
Firefox and WebKit). It is not disguised: it reports `navigator.webdriver`, and
in headless mode its user agent says so. That is fine for pages that need
JavaScript and wrong for pages that refuse automated visitors — use
`stealth=True` for those.

### Finding a page's API

A rendered page records every request it made. The `fetch()`/XHR ones are
usually where its data comes from:

```python
page = s.render("https://example.com/app", wait_until="networkidle")
for call in page.api_calls:
    print(call.method, call.status_code, call.content_type, call.url)

data = s.fetch(page.api_calls[0].url).json()   # no browser needed from here on
```

`page.calls` has all requests (documents, scripts, images, ...) as
`NetworkCall(method, url, status_code, resource_type, content_type)`.

A page that fails to load raises `RenderError`. Connection failures and
page-load timeouts are retried like any other network error, moving to another
proxy when the session has a pool.

## Pages that fight back

```python
from wayfarer import ProxyPool, Session

pool = ProxyPool.from_range(os.environ["PROXY_HOST"], range(10000, 11000), ...)

with Session("firefox",
             stealth=True,                  # Camoufox, not the machine's own browser
             persona="me.json",             # the same visitor tomorrow
             proxy=pool,                    # identity and exit address stay together
             on_challenge="solve") as s:     # escalate before giving up
    page = s.render("https://hard-site.example/", wait_for="#results")
    data = s.fetch("https://hard-site.example/api/items").json()
    print(s.identity)          # what this visitor presents, claims and measurements apart
    print(s.persona.trust_for("hard-site.example"))    # unknown | challenged | granted | unsolved
```

`pip install 'wayfarer[stealth]'`, then `python -m camoufox fetch` (~200 MB) —
wayfarer does not download the browser for you, because it should not happen
mid-scrape.

**Stealth** (`stealth=True`) launches Camoufox, which spoofs fingerprints in the
C++ engine — `navigator.*`, WebGL, canvas, audio, screen — so they are already
wrong by the time page JavaScript can inspect them. JavaScript patching, by
comparison, can be detected by asking whether a property still behaves like the
real thing.

**A persona** is what makes it a *visitor* rather than a new anonymous browser
each run. `persona="me.json"` writes the identity out on close and reads it back
next time: the same recorded device, the same timezone and locale for the
address it exits from, the same cookies, and a note of which sites have already
accepted or rejected it. Camoufox draws a fresh random fingerprint on every
launch; persisting a drawn one is the part that lets a site see a returning
visitor.

**`on_challenge="solve"`** walks a ladder, cheapest first, and stops at the first
rung that works:

| Rung | What happens | Time | Cost |
| --- | --- | --- | --- |
| 1 | The plain HTTP request, as always | ~0.1 s | free |
| 2 | The same visitor loads it in a browser | 1–10 s | free |
| 3 | A solver service solves the CAPTCHA in the browser | 10–40 s | ~$0.60–2.50 per 1000 |
| 4 | Give up, and say so | — | — |

Rung 2 clears a lot of Cloudflare interstitials and non-interactive Turnstile
challenges without paying anyone. Rung 3 needs a key — `CAPMONSTER_API_KEY`
or `TWOCAPTCHA_API_KEY` — or an explicit `solver=CapMonster()`. When it gives up,
`ChallengeError.trail` lists every rung it tried and what each one did, including
the solver's name, the seconds it waited and the approximate cost. A "block"
(403 with no test to pass) is never solvable, and wayfarer says so rather than
billing you for trying.

```python
from wayfarer import CapMonster, Session, SolverError, TwoCaptcha

with Session("firefox", stealth=True, on_challenge="solve",
             solver=CapMonster()) as s:          # 2Captcha() speaks the same API
    ...

# Or use whichever keys are in the environment, and fall back when one gives up:
from wayfarer.solvers import solver_from_env
solver = solver_from_env()                        # CapMonster first, then 2Captcha
```

`CapMonster().balance()` is the quickest way to find out whether a key works
before blaming the site. CapMonster is tried first: it is faster, cheaper, and
the only one of the two that returns a `cf_clearance` cookie. Every solve comes
back as a `Solved` record carrying the token, the seconds waited and the
approximate cost, so spending is never silent.

A solved Cloudflare challenge returns `cf_clearance`, which Cloudflare binds to
the IP *and* the user agent that earned it. wayfarer asks the solver with the
user agent the live browser was actually sending — not the one the persona's
device record was written with, which is usually an older Firefox — then
replays the cookie over the plain path. If the plain path is still challenged,
which is most often the two Firefox versions disagreeing, `ChallengeError.trail`
says that rather than leaving you to guess why a solved page still 403s.

## Measuring the disguise

Stealth is a claim, and claims rot: a browser update, a new fingerprint
database, a detector that learns one string. So wayfarer ships the check.

```bash
python -m wayfarer diagnose --backends camoufox --json tests/baselines/camoufox.json
python -m wayfarer diagnose --backends playwright,camoufox --baseline tests/baselines/camoufox.json
python -m wayfarer doctor --balance
```

`diagnose` launches each backend, loads a page, and reports what the page could
read back about it: `navigator.webdriver`, leftover automation globals, whether
the window fits the screen it claims, whether the timezone matches the persona's,
whether the GPU string is a string a real driver would write. Each finding has a
stable code, so a regression shows up as one new code in a diff.

`doctor` answers the setup questions that cost an hour to find: whether
Camoufox's browser is actually fetched, which build, which Firefox version
`curl_cffi` is impersonating (the two have to agree for a solved cookie to
survive), and whether the solver key is live.

The same checks run as tests:

```bash
python -m pytest                                    # 90 checks, no browser or network needed
WAYFARER_LIVE=1 python -m pytest -m live            # the ones that launch Camoufox
```

The live checks are what stop a fix that silently stopped working — a rewritten
GPU string that never landed, for instance, looks exactly like a working one
until a site disagrees.

## Scope

wayfarer makes a script consistent with a real browser, and where that is not
enough it escalates in a browser that is disguised at engine level, then pays a
service for the token the site asked for. It does not forge a challenge token
it has never been given, and it does not pretend a flat block is a puzzle.

Known limits:

- `visit()` and `fetch()` run no JavaScript; use `render()` or call the page's API with `fetch()`.
- `render()` does not emulate mobile devices, so the mobile profiles stay HTTP-only.
- Camoufox is a **Firefox**. Sites that lean on Chrome or V8-specific behaviour need a real Chrome; that path is planned and is not a better version of this one.
- Camoufox's browser and `curl_cffi`'s newest Firefox target can disagree on version (158 against 155 on the machine writing this). They drift in opposite directions: Camoufox ships a beta of whatever Firefox it forked, `curl_cffi` adds a target when Firefox stabilises. `doctor` prints both; a solved cookie is only replayed when the two agree on the user agent.
- Camoufox's GPU records all end in `", or similar"` — 10 of 11 Windows entries, and every macOS one — which is not a string any real driver writes. wayfarer rewrites it into the shape Firefox reports and then reads it back out of the live page to confirm the rewrite landed.
- Camoufox re-randomises **canvas** readback on every launch rather than seeding it from the pinned identity (measured on 0.5.9: audio and WebGL parameter draws match across launches of one saved persona, the canvas hash does not). Everything else a persona pins — user agent, GPU strings, screen, timezone, locale, cookies — does carry over. A site that recognises visitors by canvas alone will see the same persona as a new visitor each run; `python -m wayfarer diagnose` is how you find out if that has changed.
- Camoufox warns on stderr that canvas noise is on, because a persona that claims another OS gets noise rather than this machine's real canvas. That is the trade we choose: a canvas belonging to a different GPU is a worse tell than a hardened one.
- CAPTCHA solving is a paid third party: 10–40 s of latency, roughly $0.60–2.50 per thousand, and solvable-but-still-blocked happens. It is opt-in, and `on_challenge` defaults to `"raise"` so nothing starts spending money on its own.
- Headers are computed for the first hop; redirects are followed by libcurl with the same headers.
- Same-site detection uses a small heuristic, not the full Public Suffix List.
- Fingerprints are only as fresh as the installed `curl_cffi` and Camoufox build; `diagnose` is how you find out, not this list.
- A session's browsing state is not designed for concurrent navigations from several threads.

## Development

```bash
pip install -e .[dev]
python -m pytest
WAYFARER_LIVE=1 python -m pytest -m live
```

`STEALTH_PLAN.md` records the measurements behind the stealth layer — what an
unmodified browser leaks here, what Camoufox leaks on its own, and the reasoning
for each fix — so a future change can disagree with the reasoning instead of
rediscovering it.
