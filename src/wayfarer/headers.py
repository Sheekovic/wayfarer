"""Context-aware request headers.

curl_cffi already sends a browser's default headers for a top-level page
load. What it cannot know is the context of each request: where the visitor
came from, and whether this is a page navigation or a fetch()/XHR call made
by a page. Real browsers express that context in Referer, Origin and the
Sec-Fetch-* headers, and a mismatch (for example an API call that claims to
be a document navigation) is an easy inconsistency to spot. This module
computes those headers from the session's browsing state.
"""

from __future__ import annotations

import ipaddress
from typing import Dict, Optional, Sequence
from urllib.parse import urlsplit, urlunsplit

from .profiles import Profile

NAVIGATE = "navigate"
FETCH = "fetch"

_DEFAULT_PORTS = {"http": 80, "https": 443}
# Without the full Public Suffix List we approximate: "example.co.uk" style
# names keep three labels, everything else keeps two.
_SECOND_LEVEL = {"co", "com", "org", "net", "gov", "ac", "edu"}


def origin(url: str) -> str:
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    host = (parts.hostname or "").lower()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and port != _DEFAULT_PORTS.get(scheme):
        host = f"{host}:{port}"
    return f"{scheme}://{host}"


def registrable_domain(host: str) -> str:
    host = host.lower().rstrip(".")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.split(".")
    if len(labels) >= 3 and len(labels[-1]) == 2 and labels[-2] in _SECOND_LEVEL:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def site_relation(initiator: Optional[str], target: str) -> str:
    """Value of Sec-Fetch-Site for a request to ``target`` made from ``initiator``."""
    if not initiator:
        return "none"
    if origin(initiator) == origin(target):
        return "same-origin"
    a, b = urlsplit(initiator), urlsplit(target)
    if a.scheme.lower() == b.scheme.lower() and registrable_domain(
        a.hostname or ""
    ) == registrable_domain(b.hostname or ""):
        return "same-site"
    return "cross-site"


def referer_for(page_url: str, target: str) -> Optional[str]:
    """Referer under the browsers' default strict-origin-when-cross-origin policy."""
    page, dest = urlsplit(page_url), urlsplit(target)
    if page.scheme.lower() not in _DEFAULT_PORTS:
        return None
    if page.scheme.lower() == "https" and dest.scheme.lower() != "https":
        return None
    if origin(page_url) == origin(target):
        netloc = origin(page_url).split("://", 1)[1]
        return urlunsplit((page.scheme.lower(), netloc, page.path or "/", page.query, ""))
    return origin(page_url) + "/"


def build_headers(
    profile: Profile,
    *,
    mode: str,
    method: str,
    url: str,
    page_url: Optional[str],
    languages: Sequence[str],
) -> Dict[str, Optional[str]]:
    """Headers to layer over the profile's defaults. ``None`` removes a default."""
    if mode not in (NAVIGATE, FETCH):
        raise ValueError(f"mode must be 'navigate' or 'fetch', not {mode!r}")
    h: Dict[str, Optional[str]] = {"Accept-Language": profile.accept_language(languages)}
    unsafe = method.upper() not in ("GET", "HEAD")

    if profile.user_agent:
        h["User-Agent"] = profile.user_agent
    if profile.family == "chromium" and profile.mobile:
        h["sec-ch-ua-mobile"] = "?1"

    if mode == NAVIGATE:
        h["Sec-Fetch-Site"] = site_relation(page_url, url)
        if page_url:
            h["Referer"] = referer_for(page_url, url)
            if unsafe:
                h["Origin"] = origin(page_url)
    else:
        # A fetch always has an initiating page. With no page visited yet,
        # assume one on the target's own origin.
        initiator = page_url or origin(url) + "/"
        relation = site_relation(initiator, url)
        h["Accept"] = "*/*"
        h["Sec-Fetch-Site"] = relation
        h["Sec-Fetch-Mode"] = "cors"
        h["Sec-Fetch-Dest"] = "empty"
        h["Sec-Fetch-User"] = None
        h["Upgrade-Insecure-Requests"] = None
        h["Referer"] = referer_for(initiator, url)
        if unsafe or relation != "same-origin":
            h["Origin"] = origin(initiator)
        if profile.fetch_priority:
            h["Priority"] = profile.fetch_priority

    # A Referer of None means "send none" (e.g. https -> http), not "remove a default".
    if h.get("Referer", "") is None:
        del h["Referer"]
    return {profile.header_name(k): v for k, v in h.items()}
