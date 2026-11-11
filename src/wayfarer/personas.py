"""A persona: one visitor identity, shared by both engines and resumable.

A session is one visitor, so a visitor needs one identity that everything
agrees on: the fingerprint the browser reports, the timezone and locale that
belong to the proxy it exits from, the cookies it has collected, and the trust
each site has already given it. Without that, every run is a stranger with a
fresh face, which is exactly what a site scoring repeat visitors notices.

The identity is anchored on a Camoufox fingerprint *preset* -- a real device's
recorded values -- kept here so the same persona can be handed back to the
browser on the next launch. Camoufox draws a new random fingerprint every
launch by default; persisting it is what makes a returning visitor possible.

This module does no I/O beyond reading and writing one JSON file, and imports
nothing from Camoufox, so it works without the ``stealth`` extra installed.
"""

from __future__ import annotations

import json
import os
import random
from dataclasses import dataclass, field
from datetime import datetime, timezone as _timezone
from typing import Any, Dict, Optional, Sequence, Tuple, Union

PathLike = Union[str, "os.PathLike[str]"]

#: Bumps when the on-disk shape changes; older files are still read.
SCHEMA_VERSION = 1

#: "windows" | "macos" | "linux" -- Camoufox's long OS names.
OS_NAMES = ("windows", "macos", "linux")

Trust = str  # "unknown" | "challenged" | "granted" | "unsolved"


def _now() -> str:
    return datetime.now(_timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


@dataclass
class HostTrust:
    """What one site currently thinks of this persona."""

    host: str
    visits: int = 0
    last_seen: str = ""
    #: "unknown" (never challenged) | "challenged" | "granted" | "unsolved"
    trust: Trust = "unknown"
    #: Vendor that challenged this host, once it is known.
    vendor: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "host": self.host,
            "visits": self.visits,
            "last_seen": self.last_seen,
            "trust": self.trust,
            "vendor": self.vendor,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "HostTrust":
        return cls(
            host=str(data.get("host", "")),
            visits=int(data.get("visits", 0) or 0),
            last_seen=str(data.get("last_seen", "")),
            trust=str(data.get("trust", "unknown")),
            vendor=str(data.get("vendor", "")),
        )


@dataclass
class Persona:
    """A persisted visitor identity.

    >>> persona = Persona.new(os_name="windows", languages=("en-US", "en"))
    >>> persona.save("me.json")
    >>> Persona.load("me.json").ua == persona.ua
    False        # until the browser reports it, ua is what the persona claims
    """

    #: Seed every random draw in the identity comes from, so a persona can be
    #: re-drawn from the file alone if its preset is discarded.
    seed: int
    #: "windows" | "macos" | "linux".
    os_name: str = "windows"
    #: Camoufox fingerprint preset: the recorded values of a real device
    #: (navigator, screen, webgl, speechVoices). Empty until first drawn.
    preset: Dict[str, Any] = field(default_factory=dict)
    #: Firefox major version the persona claims; set from the browser build.
    ff_version: Optional[int] = None
    #: User-Agent the browser reports, read back from the live browser.
    ua: str = ""
    locale: str = "en-US"
    languages: Tuple[str, ...] = ("en-US", "en")
    #: IANA zone, e.g. "America/Chicago". Empty means "ask the proxy's exit IP".
    timezone: str = ""
    geolocation: Optional[Tuple[float, float]] = None
    #: Proxy this identity is bound to; the persona travels with it.
    proxy: Optional[str] = None
    exit_ip: str = ""
    #: Default location of the persona file itself, used by ``save()`` when no
    #: path is given. Not a browser profile: render() opens a fresh context each
    #: time and restores cookies from the session, so continuity lives in this
    #: file rather than in a Firefox profile directory on disk.
    profile_dir: str = ""
    #: Hosts this persona has met, by hostname.
    hosts: Dict[str, HostTrust] = field(default_factory=dict)
    created_at: str = field(default_factory=_now)
    updated_at: str = field(default_factory=_now)
    #: Anything the user pinned that we could not honour, for ``doctor``.
    notes: Tuple[str, ...] = ()

    # -- construction -------------------------------------------------------

    @classmethod
    def new(cls, *, seed: Optional[int] = None, os_name: str = "windows",
            languages: Sequence[str] = ("en-US", "en"), locale: Optional[str] = None,
            timezone: str = "", proxy: Optional[str] = None) -> "Persona":
        """Make a persona. Nothing about it is drawn from the network yet."""
        if os_name not in OS_NAMES:
            raise ValueError(f"unknown os {os_name!r}; choose one of: {', '.join(OS_NAMES)}")
        langs = tuple(languages)
        if not langs:
            raise ValueError("at least one language is required")
        return cls(
            seed=int(seed if seed is not None else random.randrange(1, 2**53)),
            os_name=os_name,
            languages=langs,
            locale=locale or langs[0],
            timezone=timezone,
            proxy=proxy,
        )

    # -- the identity's claims ---------------------------------------------

    @property
    def claimed_ua(self) -> str:
        """The User-Agent this persona presents, once its preset is drawn."""
        navigator = self.preset.get("navigator") or {}
        return self.ua or str(navigator.get("userAgent") or "")

    @property
    def screen(self) -> Optional[Tuple[int, int]]:
        screen = self.preset.get("screen") or {}
        width, height = screen.get("width"), screen.get("height")
        return (int(width), int(height)) if width and height else None

    @property
    def webgl(self) -> Optional[Tuple[str, str]]:
        """(vendor, renderer) as the identity's GPU record names them."""
        webgl = self.preset.get("webgl") or {}
        vendor, renderer = webgl.get("unmaskedVendor"), webgl.get("unmaskedRenderer")
        return (str(vendor), str(renderer)) if vendor and renderer else None

    def claim(self, name: str) -> str:
        """What the persona claims for a signal, for the consistency checks."""
        return {
            "ua": self.claimed_ua,
            "os": self.os_name,
            "locale": self.locale,
            "timezone": self.timezone,
        }.get(name, "")

    def touch(self, host: str, trust: Trust = "", vendor: str = "") -> HostTrust:
        """Record a visit to ``host`` and update what it thinks of us."""
        entry = self.hosts.get(host) or HostTrust(host=host)
        entry.visits += 1
        entry.last_seen = _now()
        if trust:
            entry.trust = trust
        if vendor:
            entry.vendor = vendor
        self.hosts[host] = entry
        self.updated_at = _now()
        return entry

    def trust_for(self, host: str) -> str:
        entry = self.hosts.get(host)
        return entry.trust if entry else "unknown"

    def note(self, text: str) -> None:
        """Remember something we could not honour, once each."""
        if text not in self.notes:
            self.notes = self.notes + (text,)
            self.updated_at = _now()

    # -- persistence --------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": SCHEMA_VERSION,
            "seed": self.seed,
            "os": self.os_name,
            "preset": self.preset,
            "ff_version": self.ff_version,
            "ua": self.ua,
            "locale": self.locale,
            "languages": list(self.languages),
            "timezone": self.timezone,
            "geolocation": list(self.geolocation) if self.geolocation else None,
            "proxy": self.proxy,
            "exit_ip": self.exit_ip,
            "profile_dir": self.profile_dir,
            "hosts": {host: entry.to_dict() for host, entry in self.hosts.items()},
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Persona":
        geolocation = data.get("geolocation")
        return cls(
            seed=int(data.get("seed") or 0),
            os_name=str(data.get("os") or "windows"),
            preset=dict(data.get("preset") or {}),
            ff_version=data.get("ff_version"),
            ua=str(data.get("ua") or ""),
            locale=str(data.get("locale") or "en-US"),
            languages=tuple(data.get("languages") or ("en-US", "en")),
            timezone=str(data.get("timezone") or ""),
            geolocation=(float(geolocation[0]), float(geolocation[1])) if geolocation else None,
            proxy=data.get("proxy"),
            exit_ip=str(data.get("exit_ip") or ""),
            profile_dir=str(data.get("profile_dir") or ""),
            hosts={
                str(host): HostTrust.from_dict(entry)
                for host, entry in (data.get("hosts") or {}).items()
            },
            created_at=str(data.get("created_at") or _now()),
            updated_at=str(data.get("updated_at") or _now()),
            notes=tuple(data.get("notes") or ()),
        )

    def save(self, path: Optional[PathLike] = None) -> PathLike:
        """Write the persona as JSON, atomically, so a crash cannot truncate it."""
        target = path or self.profile_dir or "persona.json"
        tmp = f"{os.fspath(target)}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        os.replace(tmp, target)
        return target

    @classmethod
    def load(cls, path: PathLike) -> "Persona":
        with open(path, encoding="utf-8") as f:
            return cls.from_dict(json.load(f))


def open_persona(persona: Union[Persona, str, PathLike, None]) -> Optional[Persona]:
    """Accept a Persona, a path to one, or None; a missing file starts fresh."""
    if persona is None or isinstance(persona, Persona):
        return persona  # type: ignore[return-value]
    path = os.fspath(persona)
    if os.path.exists(path):
        return Persona.load(path)
    return None


def resolve(persona: Persona, *, browser_version: Optional[str] = None,
            exit_ip: str = "", timezone: str = "", geolocation: Optional[Tuple[float, float]] = None,
            ua: str = "") -> Persona:
    """Fold what the live browser and proxy turned out to be into the persona."""
    if browser_version:
        major = str(browser_version).split(".", 1)[0]
        if major.isdigit():
            persona.ff_version = int(major)
    if exit_ip:
        persona.exit_ip = exit_ip
    if timezone and not persona.timezone:
        persona.timezone = timezone
    if geolocation and persona.geolocation is None:
        persona.geolocation = (float(geolocation[0]), float(geolocation[1]))
    if ua:
        persona.ua = ua
    persona.updated_at = _now()
    return persona
