"""Sync and async sessions."""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence, Union
from urllib.parse import urlsplit

from curl_cffi import requests as _curl
from curl_cffi.requests.exceptions import ConnectionError as _ConnectionError
from curl_cffi.requests.exceptions import ProxyError as _ProxyError
from curl_cffi.requests.exceptions import Timeout as _Timeout

from .challenges import challenge_kind, detect_challenge, extract_sitekey, looks_challenged
from .browser import AsyncBrowserHost, BrowserHost, RenderedPage
from .camoufox_host import AsyncCamoufoxBrowserHost, CamoufoxBrowserHost
from .cookies import PathLike, dump_cookies, from_browser, load_cookies, to_browser
from .errors import ChallengeError, RenderError, RobotsDisallowed
from .headers import FETCH, NAVIGATE, build_headers, referer_for
from .pacing import Delay, HostPacer, RetryPolicy
from .personas import Persona, open_persona
from .profiles import Profile, get_profile
from .proxies import ProxyPool
from .robots import RobotsCache
from .solvers import SolverError, apply_cookies, apply_token, aapply_cookies, aapply_token, solver_from_env

_RETRYABLE_ERRORS = (_ConnectionError, _Timeout, _ProxyError)
ProxySpec = Union[str, ProxyPool, None]
HeaderMap = Mapping[str, Optional[str]]
OnChallenge = ("raise", "ignore", "solve")


def _persona_for(persona: Any, stealth: bool, languages: Sequence[str]) -> Optional[Persona]:
    """Resolve ``persona=``: an identity, a file holding one, or a fresh one.

    A path that does not exist yet is not an error -- it is the first visit this
    identity will ever make, and the file appears on close.
    """
    if not stealth and persona is None:
        return None
    loaded = open_persona(persona)
    if loaded is None:
        return Persona.new(languages=tuple(languages))
    return loaded


class _Core:
    """Everything a session decides that does not involve I/O."""

    def __init__(
        self,
        profile: Union[str, Profile],
        languages: Sequence[str],
        delay: Delay,
        retry: Optional[RetryPolicy],
        on_challenge: str,
        respect_robots: bool,
        robots_agent: str,
        cookie_file: Optional[PathLike],
        proxy: ProxySpec = None,
        timeout: float = 30.0,
        stealth: bool = False,
        persona: Optional[Persona] = None,
        solver: Any = None,
    ) -> None:
        if on_challenge not in OnChallenge:
            raise ValueError(f"on_challenge must be one of: {', '.join(OnChallenge)}")
        self.profile = get_profile(profile)
        self.languages = tuple(languages)
        self.pacer = HostPacer(delay)
        self.retry = retry if retry is not None else RetryPolicy()
        self.on_challenge = on_challenge
        self.robots = RobotsCache(robots_agent) if respect_robots else None
        self.cookie_file = cookie_file
        self.timeout = timeout
        #: URL of the page the visitor is currently "on".
        self.page_url: Optional[str] = None
        self.pool = proxy if isinstance(proxy, ProxyPool) else None
        #: The proxy this session currently uses (sticky for its lifetime).
        self.proxy: Optional[str] = self.pool.pick() if self.pool else proxy
        self._failed_proxies: set = set()
        #: A disguised browser instead of an honest one, and the identity it wears.
        self.stealth = stealth
        self.persona = persona
        #: A CAPTCHA solver; None means pay nobody and report what was found.
        self.solver = solver
        # An identity and the address it exits from belong together: the
        # timezone and location a browser claims come from that address.
        if persona is not None and self.proxy and not persona.proxy:
            persona.proxy = self.proxy

    def with_proxy(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        if self.proxy is None or "proxy" in kwargs or "proxies" in kwargs:
            return kwargs
        return {**kwargs, "proxy": self.proxy}

    def render_options(self, url: str, wait_until: str, wait_for: Optional[str],
                       timeout: Optional[float], headless: bool) -> Dict[str, Any]:
        # In stealth mode the browser's language is part of the identity, so it
        # comes from the persona rather than from whatever we asked HTTP for.
        locale = self.persona.locale if self.persona is not None else self.languages[0]
        return dict(
            proxy=self.proxy,
            locale=locale,
            referer=referer_for(self.page_url, url) if self.page_url else None,
            wait_until=wait_until,
            wait_for=wait_for,
            timeout=timeout if timeout is not None else self.timeout,
            headless=headless,
        )

    def proxy_failed(self) -> None:
        """After a network error: move a pooled session to another proxy."""
        if self.pool is None or len(self.pool) < 2 or self.proxy is None:
            return
        self._failed_proxies.add(self.proxy)
        if len(self._failed_proxies) >= len(self.pool):
            self._failed_proxies = {self.proxy}
        self.proxy = self.pool.pick(exclude=self._failed_proxies)

    @staticmethod
    def resolve_mode(mode: str, kwargs: Dict[str, Any]) -> str:
        if mode == "auto":
            return FETCH if kwargs.get("json") is not None else NAVIGATE
        return mode

    @staticmethod
    def host(url: str) -> str:
        return (urlsplit(url).hostname or "").lower()

    def headers_for(
        self, method: str, url: str, mode: str, extra: Optional[HeaderMap]
    ) -> Dict[str, Optional[str]]:
        headers = build_headers(
            self.profile,
            mode=mode,
            method=method,
            url=url,
            page_url=self.page_url,
            languages=self.languages,
        )
        if extra:
            overridden = {k.lower() for k in extra}
            headers = {k: v for k, v in headers.items() if k.lower() not in overridden}
            headers.update(extra)
        return headers

    def inspect(self, response: Any) -> None:
        if self.on_challenge == "raise":
            challenge = detect_challenge(response)
            if challenge is not None:
                raise ChallengeError(challenge, response)

    def retry_wait(self, attempt: int, method: str, response: Any = None) -> Optional[float]:
        if response is None:
            return self.retry.wait_before_retry(attempt, method)
        return self.retry.wait_before_retry(
            attempt,
            method,
            status=response.status_code,
            retry_after=response.headers.get("retry-after"),
        )

    def record(self, response: Any, mode: str) -> None:
        if mode == NAVIGATE and response.status_code < 400:
            self.page_url = response.url

    def robots_verdict(self, url: str) -> Optional[bool]:
        """None when robots.txt for this origin still has to be fetched."""
        if self.robots is None or urlsplit(url).path == "/robots.txt":
            return True
        return self.robots.allowed(url)

    def robots_store(self, url: str, response: Any) -> None:
        assert self.robots is not None
        if response is None:
            self.robots.store(url, None)
        else:
            self.robots.store(url, response.status_code, response.text)
        delay = self.robots.crawl_delay(url)
        if delay:
            self.pacer.set_floor(self.host(url), delay)


class _Common:
    _core: _Core
    _raw: Any
    _browser: Any
    _persona_path: Optional[Any] = None

    @property
    def profile(self) -> Profile:
        return self._core.profile

    @property
    def stealth(self) -> bool:
        """True when renders happen in a disguised browser."""
        return self._core.stealth

    @property
    def persona(self) -> Optional[Persona]:
        """The identity a stealth session wears, or None."""
        return self._core.persona

    @property
    def signals(self) -> Dict[str, Any]:
        """What the last rendered page could read about this browser.

        Empty until something has been rendered: it is a measurement, not a
        claim, so a session that has not visited a page has nothing to say.
        """
        return dict(getattr(self._browser, "signals", {}) or {})

    @property
    def identity(self) -> Dict[str, Any]:
        """What this visitor presents, as far as it is known.

        Claims (what the persona means to present) and measurements (what a
        page actually read) are kept apart on purpose: the gap between them is
        the list of things a detector can see.
        """
        persona, signals = self._core.persona, self.signals
        return {
            "engine": "camoufox" if self._core.stealth else "playwright",
            "profile": self._core.profile.name,
            "user_agent": signals.get("userAgent") or (persona.claimed_ua if persona else ""),
            "browser_version": signals.get("userAgent", "").split("Firefox/")[-1].split(" ")[0]
            or (str(persona.ff_version) if persona and persona.ff_version else ""),
            "timezone": signals.get("timezone") or (persona.timezone if persona else ""),
            "exit_ip": persona.exit_ip if persona else "",
            "proxy": self._core.proxy,
            "measured": bool(signals),
        }

    @property
    def page_url(self) -> Optional[str]:
        """The page this session is currently on (source of Referer/Origin)."""
        return self._core.page_url

    @property
    def proxy(self) -> Optional[str]:
        """The proxy URL in use (may contain credentials), or None."""
        return self._core.proxy

    @property
    def cookies(self) -> Any:
        return self._raw.cookies

    def save_cookies(self, path: Optional[PathLike] = None) -> None:
        target = path or self._core.cookie_file
        if target is None:
            raise ValueError("no path given and the session has no cookie_file")
        dump_cookies(self._raw.cookies.jar, target)

    def load_cookies(self, path: PathLike) -> int:
        return load_cookies(self._raw.cookies.jar, path)

    def _load_cookie_file(self) -> None:
        path = self._core.cookie_file
        if path is not None and os.path.exists(path):
            self.load_cookies(path)

    def _save_persona(self) -> None:
        """Write the identity back, so the next run starts as this visitor.

        Only a persona given as a file is written: handing in a Persona object
        says "this is my identity to manage", and wayfarer does not overwrite
        files it was never pointed at.
        """
        persona, path = self._core.persona, self._persona_path
        if persona is not None and path is not None:
            persona.save(path)


class Session(_Common):
    """A browser-like visitor.

    >>> with Session("chrome") as s:
    ...     page = s.visit("https://example.com/")
    ...     data = s.fetch("https://example.com/api/items").json()

    Parameters
    ----------
    profile:
        "chrome", "firefox", "safari", "chrome_android", "safari_ios", or a
        :class:`Profile`. Sets the TLS/HTTP2 handshake and matching headers.
    languages:
        Preferred languages, most preferred first.
    delay:
        Seconds between requests to the same host: a number or a
        ``(low, high)`` range picked from at random. ``0`` disables pacing.
    retry:
        A :class:`RetryPolicy`; the default retries idempotent requests on
        429/502/503/504 and network errors, honouring Retry-After.
    on_challenge:
        "raise" (default) raises :class:`ChallengeError` when the site
        answers with a bot challenge; "ignore" returns the response as is;
        "solve" walks the escalation ladder -- a browser load as the same
        visitor, then a CAPTCHA solver if one is configured -- and raises
        :class:`ChallengeError` with the trail of what it tried if none of it
        worked.
    respect_robots:
        Check robots.txt before each request and apply its Crawl-delay.
    cookie_file:
        JSON file to load cookies from on start and save them to on close.
    proxy:
        A proxy URL, or a :class:`ProxyPool`. From a pool the session takes
        one proxy and keeps it, moving to another only after a network error.
    stealth:
        Render in a Camoufox browser whose fingerprint is spoofed in the
        engine, instead of the machine's own browser. Needs ``wayfarer[stealth]``.
    persona:
        A :class:`~wayfarer.personas.Persona`, or a file holding one. A file is
        loaded if it exists and written back on close, so the next run is the
        same visitor. Only used when ``stealth`` is on.
    solver:
        A CAPTCHA solver (:class:`~wayfarer.solvers.CapMonster`,
        :class:`~wayfarer.solvers.TwoCaptcha`, or a chain). With "solve" and no
        solver given, the keys in the environment are used.
    humanize, pin_cpu_cores, block_images:
        Passed to Camoufox. ``humanize`` moves the mouse like a person does;
        ``block_images`` skips image downloads, which is faster and one fewer
        thing the page can time.
    timeout, **session_options:
        Passed through to ``curl_cffi.requests.Session``.
    """

    def __init__(
        self,
        profile: Union[str, Profile] = "chrome",
        *,
        languages: Sequence[str] = ("en-US", "en"),
        delay: Delay = (0.5, 1.5),
        retry: Optional[RetryPolicy] = None,
        on_challenge: str = "raise",
        respect_robots: bool = False,
        robots_agent: str = "*",
        cookie_file: Optional[PathLike] = None,
        proxy: ProxySpec = None,
        timeout: float = 30.0,
        stealth: bool = False,
        persona: Union[Persona, str, Any, None] = None,
        solver: Any = None,
        humanize: bool = True,
        pin_cpu_cores: bool = False,
        block_images: bool = False,
        **session_options: Any,
    ) -> None:
        self._core = _Core(profile, languages, delay, retry, on_challenge,
                           respect_robots, robots_agent, cookie_file, proxy, timeout,
                           stealth, _persona_for(persona, stealth, languages),
                           solver)
        self._raw = _curl.Session(
            impersonate=self._core.profile.impersonate,
            timeout=timeout,
            **session_options,
        )
        self._persona_path = persona if isinstance(persona, (str, os.PathLike)) else None
        self._solved: set = set()
        self._trail: List[str] = []
        self._browser = (
            CamoufoxBrowserHost(self._core.persona or Persona.new(), humanize=humanize,
                               pin_cpu_cores=pin_cpu_cores, block_images=block_images)
            if stealth else BrowserHost(self._core.profile)
        )
        self._load_cookie_file()

    # -- core ---------------------------------------------------------------

    def _send(self, method: str, url: str, mode: str,
              headers: Optional[HeaderMap], kwargs: Dict[str, Any]) -> Any:
        wait = self._core.pacer.reserve(self._core.host(url))
        if wait > 0:
            time.sleep(wait)
        return self._raw.request(
            method, url, headers=self._core.headers_for(method, url, mode, headers),
            **self._core.with_proxy(kwargs),
        )

    def _check_robots(self, url: str) -> None:
        core = self._core
        verdict = core.robots_verdict(url)
        if verdict is None:
            robots_url = RobotsCache.robots_url(url)
            try:
                response = self._send("GET", robots_url, NAVIGATE, None, {})
            except _curl.RequestsError:
                response = None
            core.robots_store(url, response)
            verdict = core.robots_verdict(url)
        if not verdict:
            raise RobotsDisallowed(url)

    def request(self, method: str, url: str, *, mode: str = "auto",
                headers: Optional[HeaderMap] = None, **kwargs: Any) -> Any:
        """Send a request and return a ``curl_cffi`` Response.

        ``mode`` is "navigate" (a page load: the session moves to that page),
        "fetch" (a fetch()/XHR made from the current page), or "auto"
        (fetch when ``json=`` is given, otherwise navigate).
        """
        core = self._core
        mode = core.resolve_mode(mode, kwargs)
        self._check_robots(url)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self._send(method, url, mode, headers, kwargs)
            except _RETRYABLE_ERRORS:
                core.proxy_failed()
                wait = core.retry_wait(attempt, method)
                if wait is None:
                    raise
                time.sleep(wait)
                continue
            if core.on_challenge == "solve":
                response = self._after_challenge(method, url, mode, headers, kwargs, response)
            else:
                core.inspect(response)
            wait = core.retry_wait(attempt, method, response)
            if wait is None:
                core.record(response, mode)
                return response
            time.sleep(wait)

    # -- escalation ---------------------------------------------------------

    def _after_challenge(self, method: str, url: str, mode: str, headers: Optional[HeaderMap],
                         kwargs: Dict[str, Any], response: Any) -> Any:
        """Walk the rungs above plain HTTP, or raise with the trail of what was tried.

        The ladder is short on purpose: a browser load as the same visitor is
        free, a solver is not, and rotating to a new identity is a decision that
        belongs to the caller, who knows what an IP costs.
        """
        core = self._core
        challenge = detect_challenge(response)
        if challenge is None:
            return response
        host = core.host(url)
        self._trail = [f"{challenge.vendor} {challenge.kind} over plain HTTP "
                       f"(HTTP {getattr(response, 'status_code', '?')})"]
        if core.persona is not None:
            core.persona.touch(host, "challenged", challenge.vendor)
        if host in self._solved:
            self._trail.append("the browser ladder was already walked for this host")
            raise ChallengeError(challenge, response, self._trail)
        self._solved.add(host)
        self._trail.append("loading it in a browser as the same visitor")
        page = self.render(url, on_page=self._solve_hook)
        fresh = detect_challenge(page)
        if fresh is None:
            self._trail.append("it loaded; its cookies now belong to the plain path")
            replay = self._send(method, url, mode, headers, kwargs)
            still = detect_challenge(replay)
            if still is not None:
                self._trail.append("the plain path was still challenged after the browser got through")
                raise ChallengeError(still, replay, self._trail)
            return replay
        self._trail.append(f"still {fresh.vendor} after a browser load")
        if core.persona is not None:
            core.persona.touch(host, "unsolved", fresh.vendor)
        raise ChallengeError(fresh, page, self._trail)

    def _solve_hook(self, page: Any, snapshot: RenderedPage) -> bool:
        """Ask a solver for the token this page is waiting on, and hand it back."""
        core = self._core
        challenge = detect_challenge(snapshot)
        if challenge is None:
            return True
        html = snapshot.html or ""
        solver = core.solver or solver_from_env()
        if solver is None:
            self._trail.append(
                f"{challenge.vendor} wants more than a browser: pass solver= or set CAPMONSTER_API_KEY")
            return False
        kind = challenge_kind(html) or ("cf_clearance" if challenge.vendor == "Cloudflare" else "")
        if not kind:
            self._trail.append(f"cannot tell which challenge {challenge.vendor} is showing")
            return False
        sitekey = extract_sitekey(html) or ""
        if kind != "cf_clearance" and not sitekey:
            self._trail.append(f"the {kind} widget does not name a sitekey")
            return False
        try:
            solved = solver.solve(kind, sitekey=sitekey, url=snapshot.url, proxy=core.proxy,
                                   user_agent=self.identity["user_agent"], page_html=html)
        except SolverError as exc:
            self._trail.append(f"solver failed: {exc}")
            return False
        self._trail.append(f"{solved.provider} solved {kind} in {solved.seconds}s "
                           f"(~${solved.usd:.5f})")
        domain = urlsplit(snapshot.url).hostname or ""
        try:
            if solved.cookies:
                apply_cookies(page, solved.cookies, domain)
                page.reload(wait_until="load", timeout=core.timeout * 1000)
            elif not apply_token(page, kind, solved.token):
                self._trail.append(f"the page has no field for a {kind} token")
                return False
            try:
                page.wait_for_load_state("load", timeout=core.timeout * 1000)
            except Exception:
                pass
        except Exception as exc:
            self._trail.append(f"the token could not be handed to the page: {exc}")
            return False
        if looks_challenged(page.content()):
            self._trail.append(f"the page still shows a {kind} challenge after the token")
            return False
        return True

    # -- conveniences -------------------------------------------------------

    def visit(self, url: str, **kwargs: Any) -> Any:
        """Load a page, as if typed in the address bar or followed from the current page."""
        return self.request("GET", url, mode=NAVIGATE, **kwargs)

    def fetch(self, url: str, method: str = "GET", **kwargs: Any) -> Any:
        """Call a URL the way the current page's JavaScript would."""
        return self.request(method, url, mode=FETCH, **kwargs)

    def render(self, url: str, *, wait_until: str = "load", wait_for: Optional[str] = None,
               timeout: Optional[float] = None, headless: bool = True,
               on_page: Optional[Any] = None) -> RenderedPage:
        """Load a page in a real browser and return it after its JavaScript ran.

        Uses the session's cookies, proxy and language, and writes cookies the
        page sets back to the session, so ``visit()``/``fetch()`` continue as
        the same visitor. ``wait_for`` is a CSS selector to wait for.
        ``on_page(page, snapshot)`` runs while the page is still open, which is
        how a challenge gets solved in the identity that was served it. The
        browser is unmodified unless ``stealth`` is on. Requires the ``browser``
        extra (Playwright), or the ``stealth`` extra (Camoufox).
        """
        core = self._core
        self._check_robots(url)
        attempt = 0
        while True:
            attempt += 1
            wait = core.pacer.reserve(core.host(url))
            if wait > 0:
                time.sleep(wait)
            try:
                page, cookies = self._browser.render(
                    url, cookies=to_browser(self._raw.cookies.jar), on_page=on_page,
                    **core.render_options(url, wait_until, wait_for, timeout, headless),
                )
                break
            except RenderError as exc:
                if not exc.network:
                    raise
                core.proxy_failed()
                wait = core.retry_wait(attempt, "GET")
                if wait is None:
                    raise
                time.sleep(wait)
        from_browser(self._raw.cookies.jar, cookies)
        core.inspect(page)
        core.record(page, NAVIGATE)
        return page

    def get(self, url: str, **kwargs: Any) -> Any:
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any) -> Any:
        return self.request("HEAD", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> Any:
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any) -> Any:
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any) -> Any:
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> Any:
        return self.request("DELETE", url, **kwargs)

    def close(self) -> None:
        try:
            self._save_persona()
            if self._core.cookie_file is not None:
                self.save_cookies()
        finally:
            try:
                self._browser.close()
            finally:
                self._raw.close()

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


class AsyncSession(_Common):
    """Async twin of :class:`Session`; same options, awaitable methods."""

    def __init__(
        self,
        profile: Union[str, Profile] = "chrome",
        *,
        languages: Sequence[str] = ("en-US", "en"),
        delay: Delay = (0.5, 1.5),
        retry: Optional[RetryPolicy] = None,
        on_challenge: str = "raise",
        respect_robots: bool = False,
        robots_agent: str = "*",
        cookie_file: Optional[PathLike] = None,
        proxy: ProxySpec = None,
        timeout: float = 30.0,
        stealth: bool = False,
        persona: Union[Persona, str, Any, None] = None,
        solver: Any = None,
        humanize: bool = True,
        pin_cpu_cores: bool = False,
        block_images: bool = False,
        **session_options: Any,
    ) -> None:
        self._core = _Core(profile, languages, delay, retry, on_challenge,
                           respect_robots, robots_agent, cookie_file, proxy, timeout,
                           stealth, _persona_for(persona, stealth, languages), solver)
        self._raw = _curl.AsyncSession(
            impersonate=self._core.profile.impersonate,
            timeout=timeout,
            **session_options,
        )
        self._robots_lock = asyncio.Lock()
        self._persona_path = persona if isinstance(persona, (str, os.PathLike)) else None
        self._solved: set = set()
        self._trail: List[str] = []
        self._browser = (
            AsyncCamoufoxBrowserHost(self._core.persona or Persona.new(), humanize=humanize,
                                     pin_cpu_cores=pin_cpu_cores, block_images=block_images)
            if stealth else AsyncBrowserHost(self._core.profile)
        )
        self._load_cookie_file()

    async def _send(self, method: str, url: str, mode: str,
                    headers: Optional[HeaderMap], kwargs: Dict[str, Any]) -> Any:
        wait = self._core.pacer.reserve(self._core.host(url))
        if wait > 0:
            await asyncio.sleep(wait)
        return await self._raw.request(
            method, url, headers=self._core.headers_for(method, url, mode, headers),
            **self._core.with_proxy(kwargs),
        )

    async def _check_robots(self, url: str) -> None:
        core = self._core
        verdict = core.robots_verdict(url)
        if verdict is None:
            async with self._robots_lock:
                verdict = core.robots_verdict(url)
                if verdict is None:
                    robots_url = RobotsCache.robots_url(url)
                    try:
                        response = await self._send("GET", robots_url, NAVIGATE, None, {})
                    except _curl.RequestsError:
                        response = None
                    core.robots_store(url, response)
                    verdict = core.robots_verdict(url)
        if not verdict:
            raise RobotsDisallowed(url)

    async def request(self, method: str, url: str, *, mode: str = "auto",
                      headers: Optional[HeaderMap] = None, **kwargs: Any) -> Any:
        core = self._core
        mode = core.resolve_mode(mode, kwargs)
        await self._check_robots(url)
        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._send(method, url, mode, headers, kwargs)
            except _RETRYABLE_ERRORS:
                core.proxy_failed()
                wait = core.retry_wait(attempt, method)
                if wait is None:
                    raise
                await asyncio.sleep(wait)
                continue
            if core.on_challenge == "solve":
                response = await self._after_challenge(method, url, mode, headers, kwargs, response)
            else:
                core.inspect(response)
            wait = core.retry_wait(attempt, method, response)
            if wait is None:
                core.record(response, mode)
                return response
            await asyncio.sleep(wait)

    async def _after_challenge(self, method: str, url: str, mode: str, headers: Optional[HeaderMap],
                               kwargs: Dict[str, Any], response: Any) -> Any:
        """Awaitable twin of :meth:`Session._after_challenge`."""
        core = self._core
        challenge = detect_challenge(response)
        if challenge is None:
            return response
        host = core.host(url)
        self._trail = [f"{challenge.vendor} {challenge.kind} over plain HTTP "
                       f"(HTTP {getattr(response, 'status_code', '?')})"]
        if core.persona is not None:
            core.persona.touch(host, "challenged", challenge.vendor)
        if host in self._solved:
            self._trail.append("the browser ladder was already walked for this host")
            raise ChallengeError(challenge, response, self._trail)
        self._solved.add(host)
        self._trail.append("loading it in a browser as the same visitor")
        page = await self.render(url, on_page=self._solve_hook)
        fresh = detect_challenge(page)
        if fresh is None:
            self._trail.append("it loaded; its cookies now belong to the plain path")
            replay = await self._send(method, url, mode, headers, kwargs)
            still = detect_challenge(replay)
            if still is not None:
                self._trail.append("the plain path was still challenged after the browser got through")
                raise ChallengeError(still, replay, self._trail)
            return replay
        self._trail.append(f"still {fresh.vendor} after a browser load")
        if core.persona is not None:
            core.persona.touch(host, "unsolved", fresh.vendor)
        raise ChallengeError(fresh, page, self._trail)

    async def _solve_hook(self, page: Any, snapshot: RenderedPage) -> bool:
        """Awaitable twin of :meth:`Session._solve_hook`; the solver call blocks a thread."""
        core = self._core
        challenge = detect_challenge(snapshot)
        if challenge is None:
            return True
        html = snapshot.html or ""
        solver = core.solver or solver_from_env()
        if solver is None:
            self._trail.append(
                f"{challenge.vendor} wants more than a browser: pass solver= or set CAPMONSTER_API_KEY")
            return False
        kind = challenge_kind(html) or ("cf_clearance" if challenge.vendor == "Cloudflare" else "")
        if not kind:
            self._trail.append(f"cannot tell which challenge {challenge.vendor} is showing")
            return False
        sitekey = extract_sitekey(html) or ""
        if kind != "cf_clearance" and not sitekey:
            self._trail.append(f"the {kind} widget does not name a sitekey")
            return False
        try:
            solved = await asyncio.to_thread(
                solver.solve, kind, sitekey=sitekey, url=snapshot.url, proxy=core.proxy,
                user_agent=self.identity["user_agent"], page_html=html)
        except SolverError as exc:
            self._trail.append(f"solver failed: {exc}")
            return False
        self._trail.append(f"{solved.provider} solved {kind} in {solved.seconds}s "
                           f"(~${solved.usd:.5f})")
        domain = urlsplit(snapshot.url).hostname or ""
        try:
            if solved.cookies:
                await aapply_cookies(page, solved.cookies, domain)
                await page.reload(wait_until="load", timeout=core.timeout * 1000)
            elif not await aapply_token(page, kind, solved.token):
                self._trail.append(f"the page has no field for a {kind} token")
                return False
            try:
                await page.wait_for_load_state("load", timeout=core.timeout * 1000)
            except Exception:
                pass
        except Exception as exc:
            self._trail.append(f"the token could not be handed to the page: {exc}")
            return False
        if looks_challenged(await page.content()):
            self._trail.append(f"the page still shows a {kind} challenge after the token")
            return False
        return True

    async def visit(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, mode=NAVIGATE, **kwargs)

    async def fetch(self, url: str, method: str = "GET", **kwargs: Any) -> Any:
        return await self.request(method, url, mode=FETCH, **kwargs)

    async def render(self, url: str, *, wait_until: str = "load", wait_for: Optional[str] = None,
                     timeout: Optional[float] = None, headless: bool = True,
                     on_page: Optional[Any] = None) -> RenderedPage:
        """Load a page in a real browser and return it after its JavaScript ran.

        Uses the session's cookies, proxy and language, and writes cookies the
        page sets back to the session, so ``visit()``/``fetch()`` continue as
        the same visitor. ``wait_for`` is a CSS selector to wait for;
        ``on_page(page, snapshot)`` may be a coroutine and runs while the page
        is still open. Requires the ``browser`` extra, or ``stealth``.
        """
        core = self._core
        await self._check_robots(url)
        attempt = 0
        while True:
            attempt += 1
            wait = core.pacer.reserve(core.host(url))
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                page, cookies = await self._browser.render(
                    url, cookies=to_browser(self._raw.cookies.jar), on_page=on_page,
                    **core.render_options(url, wait_until, wait_for, timeout, headless),
                )
                break
            except RenderError as exc:
                if not exc.network:
                    raise
                core.proxy_failed()
                wait = core.retry_wait(attempt, "GET")
                if wait is None:
                    raise
                await asyncio.sleep(wait)
        from_browser(self._raw.cookies.jar, cookies)
        core.inspect(page)
        core.record(page, NAVIGATE)
        return page

    async def get(self, url: str, **kwargs: Any) -> Any:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs: Any) -> Any:
        return await self.request("HEAD", url, **kwargs)

    async def post(self, url: str, **kwargs: Any) -> Any:
        return await self.request("POST", url, **kwargs)

    async def put(self, url: str, **kwargs: Any) -> Any:
        return await self.request("PUT", url, **kwargs)

    async def patch(self, url: str, **kwargs: Any) -> Any:
        return await self.request("PATCH", url, **kwargs)

    async def delete(self, url: str, **kwargs: Any) -> Any:
        return await self.request("DELETE", url, **kwargs)

    async def close(self) -> None:
        try:
            self._save_persona()
            if self._core.cookie_file is not None:
                self.save_cookies()
        finally:
            try:
                await self._browser.close()
            finally:
                await self._raw.close()

    async def __aenter__(self) -> "AsyncSession":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()
