"""Save and restore a cookie jar as JSON, so a visit can resume later."""

from __future__ import annotations

import json
import os
import time
from http.cookiejar import Cookie, CookieJar
from typing import Iterable, List, Optional, Union

PathLike = Union[str, "os.PathLike[str]"]


def dump_cookies(jar: CookieJar, path: PathLike) -> None:
    data = [
        {
            "name": c.name,
            "value": c.value,
            "domain": c.domain,
            "path": c.path,
            "secure": bool(c.secure),
            "expires": c.expires,
            "http_only": c.has_nonstandard_attr("HttpOnly"),
        }
        for c in jar
    ]
    tmp = f"{os.fspath(path)}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def load_cookies(jar: CookieJar, path: PathLike) -> int:
    """Add unexpired cookies from ``path`` to ``jar``; returns how many were added."""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    now = time.time()
    count = 0
    for item in data:
        expires = item.get("expires")
        if expires is not None and expires <= now:
            continue
        jar.set_cookie(make_cookie(item["name"], item["value"], item["domain"],
                                   item.get("path"), item.get("secure"), expires,
                                   item.get("http_only")))
        count += 1
    return count


def make_cookie(name: str, value: str, domain: str, path: Optional[str] = "/",
                secure: Optional[bool] = False, expires: Optional[float] = None,
                http_only: Optional[bool] = False) -> Cookie:
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=bool(domain),
        domain_initial_dot=domain.startswith("."),
        path=path or "/",
        path_specified=True,
        secure=bool(secure),
        expires=int(expires) if expires is not None else None,
        discard=expires is None,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": ""} if http_only else {},
    )


def to_browser(jar: CookieJar) -> List[dict]:
    """Cookies in the shape Playwright's ``add_cookies`` expects."""
    out = []
    for c in jar:
        if not c.domain:
            continue
        item = {"name": c.name, "value": c.value or "", "domain": c.domain,
                "path": c.path or "/", "secure": bool(c.secure),
                "httpOnly": c.has_nonstandard_attr("HttpOnly")}
        if c.expires is not None:
            item["expires"] = float(c.expires)
        out.append(item)
    return out


def from_browser(jar: CookieJar, cookies: Iterable[dict]) -> None:
    """Merge cookies read from a Playwright context into ``jar``."""
    for c in cookies:
        expires = c.get("expires")
        if expires is not None and expires < 0:
            expires = None  # session cookie
        jar.set_cookie(make_cookie(c["name"], c["value"], c["domain"], c.get("path"),
                                   c.get("secure"), expires, c.get("httpOnly")))
