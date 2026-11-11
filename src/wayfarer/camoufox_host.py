"""Camoufox rendering: a browser whose fingerprint is spoofed in the engine.

Same contract as :mod:`wayfarer.browser` -- ``render()`` returns a
:class:`~wayfarer.browser.RenderedPage` and the cookies the page set -- so
``visit()`` and ``fetch()`` carry on as the same visitor either way. The
difference is what the page sees: a persona that was drawn once and is handed
back on every launch, a timezone that matches where the proxy exits, and a GPU
string with Camoufox's own tell removed and then read back to prove it went in.

Camoufox owns one identity per browser, so a session keeps one browser for its
lifetime and opens a fresh context per render, exactly as the Playwright host
does.
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, List, Optional, Tuple

from . import stealth
from .browser import (
    NetworkCall,
    RenderedPage,
    _context_options,
    _page_result,
    _recorder,
    _render_error,
)
from .detect import collect_signals
from .errors import RenderError, WayfarerError
from .personas import Persona, resolve


class _HostBase:
    """Shared setup for the sync and async Camoufox hosts."""

    def __init__(self, persona: Persona, *, humanize: bool = True,
                 pin_cpu_cores: bool = False, block_images: bool = False) -> None:
        self.persona = persona
        self.humanize = humanize
        self.pin_cpu_cores = pin_cpu_cores
        self.block_images = block_images
        #: What the live browser reports, read once after the first load.
        self.identity: Dict[str, Any] = {}
        #: The raw signal dump from the last rendered page, for detect.report().
        self.signals: Dict[str, Any] = {}
        #: False when Camoufox's payload could not be patched, recorded for doctor.
        self.webgl_patched: Optional[bool] = None
        self._pw: Any = None
        self._browser: Any = None
        self._headless: Optional[bool] = None

    def _kwargs(self, headless: bool, proxy: Optional[str]) -> Dict[str, Any]:
        stealth.draw_preset(self.persona)
        return stealth.launch_kwargs(
            self.persona,
            headless=headless,
            proxy=proxy,
            humanize=self.humanize,
            pin_cpu_cores=self.pin_cpu_cores,
            block_images=self.block_images,
        )

    def _build(self, headless: bool, proxy: Optional[str]) -> Dict[str, Any]:
        """Camoufox's own launch options, with the WebGL string rewritten."""
        from camoufox.utils import launch_options

        options = launch_options(**self._kwargs(headless, proxy))
        options, applied = stealth.patch_webgl(options)
        self.webgl_patched = applied
        if not applied:
            self.persona.note("could not rewrite the WebGL string; it keeps Camoufox's own")
        return options

    def _fold_identity(self, browser: Any, identity: Dict[str, Any]) -> None:
        """Record what the browser turned out to be, so the next run starts informed."""
        self.identity = identity
        resolve(
            self.persona,
            browser_version=stealth.browser_version(browser),
            ua=str(identity.get("userAgent") or ""),
            timezone=str(identity.get("timezone") or ""),
        )
        renderer = str(identity.get("webglRenderer") or "")
        if renderer and stealth.ARTIFACT in renderer:
            self.persona.note("the WebGL string still carries Camoufox's ', or similar' tell")

    def _ensure_started(self) -> None:
        if self._pw is None:
            raise WayfarerError("the Camoufox browser has not been started")

    def note_rendered(self, signals: Dict[str, Any]) -> None:
        """A page that loaded is the cheapest proof of what we actually look like.

        The persona records what the page could read, not what we meant to set,
        so a claim that did not land shows up as a note instead of a surprise.
        """
        self.signals = signals
        if not self.identity:
            self._fold_identity(self._browser, stealth.identity_from(signals))

    def _closed(self) -> Tuple[None, None]:
        browser, pw = self._browser, self._pw
        self._browser = self._pw = self._headless = None
        return browser, pw


class CamoufoxBrowserHost(_HostBase):
    """One lazily started Camoufox browser for a sync session."""

    def _ensure(self, headless: bool, proxy: Optional[str]) -> Any:
        if self._browser is not None and self._headless == headless:
            return self._browser
        self.close()
        stealth.require_camoufox()
        from camoufox.sync_api import NewBrowser
        from playwright.sync_api import sync_playwright

        options = self._build(headless, proxy)
        self._pw = sync_playwright().start()
        try:
            self._browser = NewBrowser(self._pw, from_options=options)
        except Exception as exc:
            pw, self._pw = self._pw, None
            pw.stop()
            raise RenderError(f"could not start Camoufox: {str(exc).strip().splitlines()[0]}",
                              network=False) from exc
        self._headless = headless
        return self._browser

    def render(self, url: str, *, cookies: List[dict], proxy: Optional[str], locale: str,
               referer: Optional[str], wait_until: str, wait_for: Optional[str],
               timeout: float, headless: bool, on_page: Optional[Any] = None) -> Tuple[RenderedPage, List[dict]]:
        browser = self._ensure(headless, proxy)
        from playwright.sync_api import Error

        context = browser.new_context(**_context_options(locale, proxy))
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
            if on_page is not None:
                # Runs while the page is still open, so a challenge can be solved
                # in the same identity that was served it.
                on_page(page, _page_result(page.url, response, page.content(), page.title(), calls))
            result = _page_result(page.url, response, page.content(), page.title(), calls)
            self.note_rendered(collect_signals(page))
            return result, context.cookies()
        except Error as exc:
            raise _render_error(url, exc) from exc
        finally:
            context.close()

    def close(self) -> None:
        browser, pw = self._closed()
        try:
            if browser is not None:
                browser.close()
        finally:
            if pw is not None:
                pw.stop()


class AsyncCamoufoxBrowserHost(_HostBase):
    """One lazily started Camoufox browser for an async session."""

    async def _ensure(self, headless: bool, proxy: Optional[str]) -> Any:
        if self._browser is not None and self._headless == headless:
            return self._browser
        await self.close()
        stealth.require_camoufox()
        from camoufox.async_api import AsyncNewBrowser
        from playwright.async_api import async_playwright

        options = self._build(headless, proxy)
        self._pw = await async_playwright().start()
        try:
            self._browser = await AsyncNewBrowser(self._pw, from_options=options)
        except Exception as exc:
            pw, self._pw = self._pw, None
            await pw.stop()
            raise RenderError(f"could not start Camoufox: {str(exc).strip().splitlines()[0]}",
                              network=False) from exc
        self._headless = headless
        return self._browser

    async def render(self, url: str, *, cookies: List[dict], proxy: Optional[str], locale: str,
                     referer: Optional[str], wait_until: str, wait_for: Optional[str],
                     timeout: float, headless: bool, on_page: Optional[Any] = None) -> Tuple[RenderedPage, List[dict]]:
        browser = await self._ensure(headless, proxy)
        from playwright.async_api import Error

        context = await browser.new_context(**_context_options(locale, proxy))
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
            if on_page is not None:
                outcome = on_page(page, _page_result(page.url, response, await page.content(),
                                                     await page.title(), calls))
                if asyncio.iscoroutine(outcome):
                    await outcome
            result = _page_result(page.url, response, await page.content(), await page.title(),
                                  calls)
            self.note_rendered(collect_signals(page))
            return result, await context.cookies()
        except Error as exc:
            raise _render_error(url, exc) from exc
        finally:
            await context.close()

    async def close(self) -> None:
        browser, pw = self._closed()
        try:
            if browser is not None:
                await browser.close()
        finally:
            if pw is not None:
                await pw.stop()
