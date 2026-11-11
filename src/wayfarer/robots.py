"""robots.txt bookkeeping (no I/O; the sessions do the fetching)."""

from __future__ import annotations

from typing import Dict, Optional, Union
from urllib.robotparser import RobotFileParser

from .headers import origin


class RobotsCache:
    def __init__(self, agent: str = "*") -> None:
        self.agent = agent
        # origin -> parsed rules, or a bool meaning allow-all / deny-all
        self._rules: Dict[str, Union[RobotFileParser, bool]] = {}

    @staticmethod
    def robots_url(url: str) -> str:
        return origin(url) + "/robots.txt"

    def allowed(self, url: str) -> Optional[bool]:
        """True/False once the origin's rules are known, None if not fetched yet."""
        rules = self._rules.get(origin(url))
        if rules is None:
            return None
        if isinstance(rules, bool):
            return rules
        return rules.can_fetch(self.agent, url)

    def store(self, url: str, status: Optional[int], text: str = "") -> None:
        """Record the outcome of fetching robots.txt (RFC 9309 semantics).

        ``status`` None means the file was unreachable.
        """
        key = origin(url)
        if status is None or status >= 500:
            self._rules[key] = False
        elif status >= 400:
            self._rules[key] = True
        else:
            parser = RobotFileParser()
            parser.parse(text.splitlines())
            self._rules[key] = parser

    def crawl_delay(self, url: str) -> Optional[float]:
        rules = self._rules.get(origin(url))
        if isinstance(rules, RobotFileParser):
            delay = rules.crawl_delay(self.agent)
            return float(delay) if delay is not None else None
        return None
