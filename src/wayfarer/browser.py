"""Real-browser rendering for pages that need JavaScript.

Some pages only exist after their JavaScript has run. For those, wayfarer
opens the page in a real browser through Playwright and shares the session's
cookies, proxy and language with it.

The browser is used as it is. Nothing about it is patched or disguised, so it
identifies itself as automated to any page that asks. This is for pages that
need JavaScript, not for pages that refuse automated visitors.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import unquote, urlsplit

from .detect import collect_signals, collect_signals_async
from .errors import RenderError, WayfarerError
from .profiles import Profile

_ENGINES = {"chromium": "chromium", "firefox": "firefox", "safari": "webkit"}
_INSTALL_HINT = (
    "Rendering needs Playwright: pip install 'wayfarer[browser]' "
    "(and, if no browser is installed, python -m playwright install)."
)


_MAX_CALLS = 1000


@dataclass(frozen=True)
class NetworkCall:
    """One request a rendered page made while loading."""

    method: str
    url: str
    status_code: int
    #: Playwright's resource type: "document", "xhr", "fetch", "script", "image", ...
    resource_type: str
    content_type: str = ""


def _recorder(calls: List[NetworkCall]) -> Any:
    def on_response(response: Any) -> None:
        if len(calls) >= _MAX_CALLS:
            return
        try:
            request = response.request
            calls.append(NetworkCall(
                method=request.method,
                url=response.url,
                status_code=response.status,
                resource_type=request.resource_type,
                content_type=(response.headers.get("content-type") or "").split(";")[0].strip(),
            ))
        except Exception:
            pass  # a page closing mid-callback must not break the render

    return on_response


@dataclass(frozen=True)
class RenderedPage:
    """A page after its JavaScript has run."""

    url: str
    status_code: int
    html: str
    title: str
    headers: Mapping[str, str] = field(default_factory=dict)
    #: Every request the page made while loading, in order.
    calls: Tuple[NetworkCall, ...] = ()

    @property
    def api_calls(self) -> Tuple[NetworkCall, ...]:
        """The fetch()/XHR calls the page's JavaScript made.

        These are the endpoints the page gets its data from; call them
        directly with ``session.fetch()`` instead of rendering again.
        """
        return tuple(c for c in self.calls if c.resource_type in ("xhr", "fetch"))

    @property
    def text(self) -> str:
        return self.html

    @property
    def content(self) -> bytes:
        return self.html.encode("utf-8")


def proxy_settings(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """Convert a proxy URL to Playwright's proxy option."""
    if not proxy:
        return None
    parts = urlsplit(proxy)
    server = f"{parts.scheme}://{parts.hostname}"
    if parts.port:
        server += f":{parts.port}"
    settings = {"server": server}
    if parts.username:
        settings["username"] = unquote(parts.username)
        settings["password"] = unquote(parts.password or "")
    return settings


def _engine_and_channels(profile: Profile) -> Tuple[str, List[Optional[str]]]:
    if profile.mobile:
        raise WayfarerError(
            f"render() is not available for the mobile profile {profile.name!r}; "
            "it runs a real desktop browser and does not emulate devices."
        )
    engine = _ENGINES[profile.family]
    # Prefer the user's installed Chrome, then Playwright's own build.
    return engine, (["chrome", None] if engine == "chromium" else [None])


# Connection-level failures, plus a page load (not a selector wait) timing out.
_NETWORK_MARKERS = ("net::ERR_", "NS_ERROR_", "Could not connect", "WebKit encountered",
                    "Page.goto: Timeout")


def _render_error(url: str, exc: Exception) -> RenderError:
    first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
    network = any(marker in first_line for marker in _NETWORK_MARKERS)
    return RenderError(f"could not render {url}: {first_line}", network=network)


def _context_options(locale: str, proxy: Optional[str]) -> Dict[str, Any]:
    options: Dict[str, Any] = {"locale": locale}
    settings = proxy_settings(proxy)
    if settings:
        options["proxy"] = settings
    return options


def _page_result(page_url: str, response: Any, html: str, title: str,
                 calls: List[NetworkCall]) -> RenderedPage:
    return RenderedPage(
        url=page_url,
        status_code=response.status if response is not None else 0,
        html=html,
        title=title,
        headers=dict(response.headers) if response is not None else {},
        calls=tuple(calls),
    )


class BrowserHost:
    """Owns one lazily started browser for a sync session."""

    def __init__(self, profile: Profile) -> None:
        self._profile = profile
        #: What the last rendered page could read about this browser.
        self.signals: Dict[str, Any] = {}
        self._pw: Any = None
        self._browser: Any = None
        self._headless: Optional[bool] = None

    def _ensure(self, headless: bool) -> Any:
        if self._browser is not None and self._headless == headless:
            return self._browser
        self.close()
        try:
            from playwright.sync_api import Error, sync_playwright
        except ImportError:
            raise WayfarerError(_INSTALL_HINT) from None
        engine, channels = _engine_and_channels(self._profile)
        self._pw = sync_playwright().start()
        last: Optional[Exception] = None
        for channel in channels:
            try:
                kwargs = {"channel": channel} if channel else {}
                self._browser = getattr(self._pw, engine).launch(headless=headless, **kwargs)
                self._headless = headless
                return self._browser
            except Error as exc:
                last = exc
        self.close()
        raise WayfarerError(f"could not start a {engine} browser. {_INSTALL_HINT}") from last

    def render(self, url: str, *, cookies: List[dict], proxy: Optional[str], locale: str,
               referer: Optional[str], wait_until: str, wait_for: Optional[str],
               timeout: float, headless: bool, on_page: Optional[Any] = None) -> Tuple[RenderedPage, List[dict]]:
        """Load a page, with the session's cookies and proxy, and read it back.

        ``on_page(page, snapshot)`` runs while the page is still open. A caller
        that needs to *do* something to the page -- solve a CAPTCHA, click a
        button -- uses it; a caller that only wants the HTML leaves it None.
        """
        browser = self._ensure(headless)
        from playwright.sync_api import Error

        try:
            context = browser.new_context(**_context_options(locale, proxy))
        except Error as exc:
            # A dead pooled proxy usually fails here, before there is a page at
            # all. Left unwrapped it escapes as a raw Playwright error, and the
            # one place rotation is needed is the place that would skip it.
            raise _render_error(url, exc) from exc
        try:
            if cookies:
                context.add_cookies(cookies)
            page = context.new_page()
            calls: List[NetworkCall] = []
            page.on("response", _recorder(calls))
            ms = timeout * 1000
            response = page.goto(url, wait_until=wait_until, timeout=ms, referer=referer)
            if wait_for:
                page.wait_for_selector(wait_for, timeout=ms)
            # Read the browser back before touching it, so anything done in
            # on_page acts on the identity as it was actually served.
            self.signals = collect_signals(page)
            if on_page is not None:
                on_page(page, _page_result(page.url, response, page.content(), page.title(), calls))
                calls = list(calls)
                self.signals = collect_signals(page)
            result = _page_result(page.url, response, page.content(), page.title(), calls)
            return result, context.cookies()
        except Error as exc:
            raise _render_error(url, exc) from exc
        finally:
            context.close()

    def close(self) -> None:
        browser, pw = self._browser, self._pw
        self._browser = self._pw = self._headless = None
        try:
            if browser is not None:
                browser.close()
        finally:
            if pw is not None:
                pw.stop()


class AsyncBrowserHost:
    """Owns one lazily started browser for an async session."""

    def __init__(self, profile: Profile) -> None:
        self._profile = profile
        #: What the last rendered page could read about this browser.
        self.signals: Dict[str, Any] = {}
        self._pw: Any = None
        self._browser: Any = None
        self._headless: Optional[bool] = None

    async def _ensure(self, headless: bool) -> Any:
        if self._browser is not None and self._headless == headless:
            return self._browser
        await self.close()
        try:
            from playwright.async_api import Error, async_playwright
        except ImportError:
            raise WayfarerError(_INSTALL_HINT) from None
        engine, channels = _engine_and_channels(self._profile)
        self._pw = await async_playwright().start()
        last: Optional[Exception] = None
        for channel in channels:
            try:
                kwargs = {"channel": channel} if channel else {}
                self._browser = await getattr(self._pw, engine).launch(headless=headless, **kwargs)
                self._headless = headless
                return self._browser
            except Error as exc:
                last = exc
        await self.close()
        raise WayfarerError(f"could not start a {engine} browser. {_INSTALL_HINT}") from last

    async def render(self, url: str, *, cookies: List[dict], proxy: Optional[str], locale: str,
                     referer: Optional[str], wait_until: str, wait_for: Optional[str],
                     timeout: float, headless: bool, on_page: Optional[Any] = None) -> Tuple[RenderedPage, List[dict]]:
        """Awaitable twin of :meth:`BrowserHost.render`; ``on_page`` may be a coroutine."""
        browser = await self._ensure(headless)
        from playwright.async_api import Error

        try:
            context = await browser.new_context(**_context_options(locale, proxy))
        except Error as exc:
            # See the sync twin: a dead proxy fails here, and rotation depends on
            # it arriving as a RenderError.
            raise _render_error(url, exc) from exc
        try:
            if cookies:
                await context.add_cookies(cookies)
            page = await context.new_page()
            calls: List[NetworkCall] = []
            page.on("response", _recorder(calls))
            ms = timeout * 1000
            response = await page.goto(url, wait_until=wait_until, timeout=ms, referer=referer)
            if wait_for:
                await page.wait_for_selector(wait_for, timeout=ms)
            self.signals = await collect_signals_async(page)
            if on_page is not None:
                outcome = on_page(page, _page_result(page.url, response, await page.content(),
                                                     await page.title(), calls))
                if asyncio.iscoroutine(outcome):
                    await outcome
                self.signals = await collect_signals_async(page)
            result = _page_result(page.url, response, await page.content(), await page.title(),
                                  calls)
            return result, await context.cookies()
        except Error as exc:
            raise _render_error(url, exc) from exc
        finally:
            await context.close()

    async def close(self) -> None:
        browser, pw = self._browser, self._pw
        self._browser = self._pw = self._headless = None
        try:
            if browser is not None:
                await browser.close()
        finally:
            if pw is not None:
                await pw.stop()
