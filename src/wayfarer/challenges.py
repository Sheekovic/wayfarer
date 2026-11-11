"""Recognise bot-challenge responses.

wayfarer's job ends where a site explicitly asks for proof of a real browser
or a human. This module only identifies those responses so the caller gets a
clear ChallengeError instead of silently parsing an interstitial page as if
it were the content. Nothing here attempts to solve or bypass a challenge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

_SUSPECT_STATUSES = {403, 429, 503}
_BODY_LIMIT = 200_000


@dataclass(frozen=True)
class Challenge:
    vendor: str
    #: "challenge" for an interstitial/JS/CAPTCHA page, "block" for a flat refusal.
    kind: str
    #: Whether a solver service can be handed this kind of challenge at all.
    solvable: bool = False


#: Vendors whose challenges a solver can be given. A "block" is never solvable:
#: it is a refusal, not a test, and paying a solver does not change a refusal.
_SOLVABLE = {"Cloudflare", "DataDome", "AWS WAF", "CAPTCHA"}

# Sitekeys are visible in the widget markup, so a challenge page usually names
# its own key. These patterns are checked in order of how specific they are.
_SITEKEY_PATTERNS = (
    re.compile(r'class="[^"]*(?:cf-turnstile|g-recaptcha|h-captcha|grecaptcha-badge)[^"]*"[^>]*data-sitekey="([^"]{6,})"', re.I),
    re.compile(r'data-sitekey="([^"]{6,})"[^>]*class="[^"]*(?:cf-turnstile|g-recaptcha|h-captcha)', re.I),
    re.compile(r"""['"]?sitekey['"]?\s*[:=]\s*['"]([A-Za-z0-9_\-]{10,})['"]"""),
    re.compile(r'recaptcha\.render\(\s*(?:#[\w\-]+|[^,]+)\s*,\s*\{[^}]*["\']sitekey["\']\s*:\s*["\']([A-Za-z0-9_\-]{10,})'),
)


def extract_sitekey(html: str) -> Optional[str]:
    """The sitekey a CAPTCHA widget on this page was built with, if it names one."""
    for pattern in _SITEKEY_PATTERNS:
        match = pattern.search(html or "")
        if match:
            return match.group(1)
    return None


def challenge_kind(html: str) -> Optional[str]:
    """Which CAPTCHA a page is showing, as the name a solver answers to.

    The vendor says who is blocking; the widget says what to ask the solver for.
    A page can carry a Turnstile inside a Cloudflare challenge and a reCAPTCHA
    inside a form, so both readings are needed and neither is enough alone.
    """
    text = html or ""
    if "cf-turnstile" in text or "challenges.cloudflare.com" in text:
        return "turnstile"
    if "g-recaptcha" in text or "recaptcha/api.js" in text or "grecaptcha" in text:
        return "recaptcha_v2"
    if "h-captcha" in text or "hcaptcha.com" in text:
        return "hcaptcha"
    if "funcaptcha" in text or "arkoselabs" in text:
        return "funcaptcha"
    return None


#: Vendor markers that mean "this body is a challenge page", status code aside.
_CHALLENGE_MARKERS = (
    "/cdn-cgi/challenge-platform/", "just a moment...", "attention required",
    "challenges.cloudflare.com",
    "captcha-delivery.com", "px-captcha", "_pxappid", "_incapsula_resource",
    "incapsula incident id", "awswaf", "g-recaptcha", "h-captcha", "cf-turnstile",
)


def looks_challenged(html: str) -> bool:
    """Whether a page body still reads as a challenge, whatever its status says.

    After a solve the browser has usually navigated, and a page's own body is
    the only evidence left: the status code and headers belong to the response
    that is already gone.
    """
    text = (html or "").lower()
    return any(marker in text for marker in _CHALLENGE_MARKERS)


def _found(vendor: str, kind: str) -> Challenge:
    """A challenge a solver could be pointed at, or a refusal that no solver fixes."""
    return Challenge(vendor, kind, solvable=kind == "challenge" and vendor in _SOLVABLE)


def _body(response: Any) -> str:
    try:
        content = response.content or b""
    except Exception:
        return ""
    return content[:_BODY_LIMIT].decode("utf-8", "replace").lower()


def detect_challenge(response: Any) -> Optional[Challenge]:
    """Return the Challenge a response represents, or None for a normal response."""
    headers = response.headers
    status = response.status_code

    if (headers.get("cf-mitigated") or "").lower() == "challenge":
        return _found("Cloudflare", "challenge")
    if status not in _SUSPECT_STATUSES:
        return None

    server = (headers.get("server") or "").lower()
    body = _body(response)

    if "cloudflare" in server:
        if "/cdn-cgi/challenge-platform/" in body or "just a moment..." in body:
            return _found("Cloudflare", "challenge")
        if "attention required" in body or "error code: 10" in body:
            return _found("Cloudflare", "block")
    if headers.get("x-datadome") or "captcha-delivery.com" in body:
        return _found("DataDome", "challenge")
    if "px-captcha" in body or "_pxappid" in body:
        return _found("HUMAN (PerimeterX)", "challenge")
    if "_incapsula_resource" in body or "incapsula incident id" in body:
        return _found("Imperva", "challenge")
    if "errors.edgesuite.net" in body or ("akamaighost" in server and "access denied" in body):
        return _found("Akamai", "block")
    if "awswaf" in body or headers.get("x-amzn-waf-action"):
        return _found("AWS WAF", "challenge")
    for marker in ("g-recaptcha", "h-captcha", "cf-turnstile"):
        if marker in body:
            return _found("CAPTCHA", "challenge")
    return None
