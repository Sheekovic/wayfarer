"""Exceptions raised by wayfarer."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Sequence

if TYPE_CHECKING:
    from .challenges import Challenge


class WayfarerError(Exception):
    """Base class for every error wayfarer raises itself."""


class ChallengeError(WayfarerError):
    """The site answered with a bot challenge instead of the page.

    ``.response`` is the original response and ``.challenge`` names the vendor
    and kind. ``.trail`` lists what wayfarer tried before giving up -- the
    escalation ladder it walked, and what each rung cost -- because "it is
    behind Cloudflare" is only useful advice if you can see how far it got.
    """

    def __init__(self, challenge: "Challenge", response: Any,
                 trail: Sequence[str] = ()) -> None:
        self.challenge = challenge
        self.response = response
        self.trail = tuple(trail)
        tried = f" Tried: {'; '.join(self.trail)}." if self.trail else ""
        super().__init__(
            f"{response.url} answered with a {challenge.vendor} {challenge.kind} "
            f"(HTTP {response.status_code}).{tried} This site requires a real browser "
            "or an official API for this request."
        )


class RenderError(WayfarerError):
    """The browser could not load a page in render().

    ``network`` is True for connection-level failures (reset, refused, DNS,
    proxy), which render() retries like any other network error.
    """

    def __init__(self, message: str, *, network: bool = False) -> None:
        self.network = network
        super().__init__(message)


class RobotsDisallowed(WayfarerError):
    """The site's robots.txt disallows this URL and respect_robots is on."""

    def __init__(self, url: str) -> None:
        self.url = url
        super().__init__(f"robots.txt disallows {url}")
