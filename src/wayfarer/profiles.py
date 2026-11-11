"""Browser profiles.

A profile ties together everything that has to agree for a request to look
like it came from one specific browser: the TLS and HTTP/2 handshake (done by
curl_cffi's impersonation target), the default header set that goes with it,
and the small per-family differences in how request headers are written.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Dict, Optional, Sequence, Union


@dataclass(frozen=True)
class Profile:
    name: str
    #: curl_cffi impersonation target. The family aliases ("chrome",
    #: "firefox", ...) track the newest version curl_cffi ships.
    impersonate: str
    #: "chromium", "firefox" or "safari".
    family: str
    mobile: bool = False
    #: Priority header value for fetch()/XHR requests, or None when this
    #: browser sends no Priority header.
    fetch_priority: Optional[str] = None
    #: Whether navigations carry Sec-Fetch-User / Upgrade-Insecure-Requests.
    sends_fetch_user: bool = True
    #: Safari writes header names in lower case even over HTTP/1.1.
    lowercase_headers: bool = False
    #: Replaces the impersonation target's default User-Agent when set.
    user_agent: Optional[str] = None

    def header_name(self, name: str) -> str:
        return name.lower() if self.lowercase_headers else name

    def accept_language(self, languages: Sequence[str]) -> str:
        """Format an Accept-Language value the way this browser family does."""
        langs = [lang for lang in languages if lang]
        if not langs:
            raise ValueError("at least one language is required")
        parts = [langs[0]]
        n = len(langs)
        for i, lang in enumerate(langs[1:], start=1):
            if self.family == "firefox":
                # Firefox spreads q-values evenly, rounded half up.
                q = math.floor((n - i) / n * 10 + 0.5) / 10
            else:
                q = max(0.1, round(1 - 0.1 * i, 1))
            parts.append(f"{lang};q={q:g}")
        return ",".join(parts)


def _firefox_user_agent() -> Optional[str]:
    """A Firefox user agent whose two version numbers agree.

    curl_cffi's default for its Firefox target carries an older ``rv:`` token
    than the ``Firefox/`` version it announces; real Firefox always sends the
    same number in both places.
    """
    try:
        from curl_cffi.requests.impersonate import DEFAULT_FIREFOX
    except ImportError:
        return None
    match = re.search(r"(\d+)", str(DEFAULT_FIREFOX))
    if not match:
        return None
    version = match.group(1)
    return (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{version}.0) "
            f"Gecko/20100101 Firefox/{version}.0")


PROFILES: Dict[str, Profile] = {
    p.name: p
    for p in (
        Profile("chrome", "chrome", "chromium", fetch_priority="u=1, i"),
        Profile("chrome_android", "chrome_android", "chromium", mobile=True,
                fetch_priority="u=1, i"),
        Profile("firefox", "firefox", "firefox", fetch_priority="u=4",
                user_agent=_firefox_user_agent()),
        Profile("safari", "safari", "safari", fetch_priority="u=3, i",
                sends_fetch_user=False, lowercase_headers=True),
        Profile("safari_ios", "safari_ios", "safari", mobile=True,
                fetch_priority="u=3, i", sends_fetch_user=False,
                lowercase_headers=True),
    )
}


def get_profile(profile: Union[str, Profile]) -> Profile:
    if isinstance(profile, Profile):
        return profile
    try:
        return PROFILES[profile]
    except KeyError:
        known = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown profile {profile!r}; choose one of: {known}") from None
