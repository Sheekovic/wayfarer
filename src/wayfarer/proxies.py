"""Proxy pools.

A session is one visitor, so it keeps one proxy for its whole life: the same
exit address, cookie jar and browser profile stay together. A pool only
decides which proxy a new session gets, and supplies a replacement when the
current one stops working.
"""

from __future__ import annotations

import random
from typing import Collection, Iterable, List, Optional
from urllib.parse import quote


class ProxyPool:
    def __init__(self, proxies: Iterable[str]) -> None:
        self._proxies: List[str] = list(dict.fromkeys(proxies))
        if not self._proxies:
            raise ValueError("a proxy pool needs at least one proxy")

    @classmethod
    def from_range(
        cls,
        host: str,
        ports: Iterable[int],
        *,
        username: Optional[str] = None,
        password: Optional[str] = None,
        scheme: str = "http",
    ) -> "ProxyPool":
        """Pool for a provider that exposes one host with many ports.

        >>> ProxyPool.from_range("proxy.example", range(10000, 11000),
        ...                      username="user", password="secret")
        """
        auth = ""
        if username is not None:
            auth = quote(username, safe="")
            if password is not None:
                auth += ":" + quote(password, safe="")
            auth += "@"
        return cls(f"{scheme}://{auth}{host}:{port}" for port in ports)

    def __len__(self) -> int:
        return len(self._proxies)

    def __contains__(self, proxy: object) -> bool:
        return proxy in self._proxies

    def pick(self, exclude: Collection[str] = ()) -> str:
        """A random proxy, avoiding ``exclude`` when any other is left."""
        candidates = [p for p in self._proxies if p not in exclude]
        return random.choice(candidates or self._proxies)

    def __repr__(self) -> str:  # never print credentials
        return f"<ProxyPool of {len(self._proxies)}>"
