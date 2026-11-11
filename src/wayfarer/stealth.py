"""Turning a persona into Camoufox launch options, and fixing what it leaks.

Camoufox spoofs fingerprints in the C++ engine, which is the right place for
it, and 0.5.x repairs most internal inconsistencies on its own. Two things it
cannot do for us:

* It draws a **new random fingerprint on every launch**, so a persona does not
  survive a restart. The fix is to draw a real device preset once, keep it in
  the persona, and hand that same preset back on every launch.
* Every WebGL renderer string it can emit ends in `", or similar"` -- the
  suffix is baked into the GPU catalogue (10 of 11 Windows records, all macOS
  ones), and no real Firefox driver ever reports that. It is one string
  comparison away from flagging the whole identity, so we rewrite it and then
  read it back out of the live browser to prove the rewrite landed.

Everything here is a pure function of a persona plus a few switches, except
:func:`patch_webgl`, which edits the config payload Camoufox is about to hand
the browser, and :func:`read_identity`, which asks a running browser what it
actually reports.
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .errors import WayfarerError
from .personas import Persona

#: The suffix Camoufox's GPU records carry and a real Firefox never produces.
ARTIFACT = ", or similar"
#: Camoufox's own config payload, split into environment variables by the launcher.
_CONFIG_PREFIX = "CAMOU_CONFIG_"
#: Below this many chunks the launcher used a fixed chunk size; see _chunk_size.
_WIN_CHUNK = 2047
_OTHER_CHUNK = 32767

_INSTALL_HINT = (
    "Stealth rendering needs Camoufox: pip install 'wayfarer[stealth]', then "
    "download the browser with `python -m camoufox fetch` (about 200 MB). "
    "wayfarer does not download it for you."
)


class CamoufoxMissing(WayfarerError):
    """Camoufox is not installed, or its browser has not been fetched."""


# -- availability ------------------------------------------------------------

def camoufox_version() -> Optional[str]:
    """The installed Camoufox version, or None when it is not installed."""
    try:
        from importlib.metadata import version

        return version("camoufox")
    except Exception:
        return None


def require_camoufox() -> None:
    """Fail early, with the actual fix, when Camoufox cannot be used.

    The package imports happily with no browser fetched, and the launch failure
    it then produces is a Playwright error several layers down. Asking for the
    path without downloading is the cheap check that says "run python -m
    camoufox fetch" instead.
    """
    if camoufox_version() is None:
        raise CamoufoxMissing(_INSTALL_HINT)
    try:
        from camoufox.pkgman import camoufox_path  # type: ignore[attr-defined]

        path = str(camoufox_path(download_if_missing=False))
    except Exception:
        # A layout this release does not know: let the launch report it rather
        # than claim the browser is missing when it might be right there.
        return
    if not os.path.isdir(path):
        raise CamoufoxMissing(f"Camoufox's browser is not installed (looked in {path}). {_INSTALL_HINT}")


# -- the WebGL string --------------------------------------------------------

def clean_renderer(renderer: Any) -> str:
    """Rewrite a Camoufox GPU string into the shape a real Firefox reports.

    ``ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11 vs_5_0 ps_5_0), or
    similar`` becomes ``ANGLE (Intel, Intel(R) HD Graphics 400 Direct3D11
    vs_5_0 ps_5_0, D3D11)``: the ``", or similar"`` hedge is dropped, and a
    Direct3D 11 string gains the feature-level tail ANGLE reports.
    """
    text = str(renderer or "").strip()
    if text.lower().endswith(ARTIFACT):
        text = text[: -len(ARTIFACT)].rstrip()
    if (text.startswith("ANGLE (") and text.endswith(")")
            and "vs_5_0 ps_5_0" in text and not text.endswith(", D3D11)")):
        text = f"{text[:-1]}, D3D11)"
    return text


def webgl_pairs(os_name: str) -> Tuple[Tuple[str, str], ...]:
    """The GPU pairs Camoufox can actually drive for this OS, sorted."""
    try:
        from camoufox.webgl import firefox_gpus
    except Exception:
        return ()
    try:
        return tuple(sorted(firefox_gpus(os_name)))
    except Exception:
        return ()


# -- launch options ----------------------------------------------------------

def proxy_settings(proxy: Optional[str]) -> Optional[Dict[str, str]]:
    """Playwright's proxy option for a proxy URL (Camoufox wants this shape)."""
    from .browser import proxy_settings as _settings

    return _settings(proxy)


def geo_options(persona: Persona, proxy: Optional[str], *, allow_lookup: bool = True) -> Dict[str, Any]:
    """How this persona gets a timezone and location that match where it exits.

    A persona that already knows its timezone says so; one that only knows its
    exit IP resolves it from Camoufox's bundled database, with no network call;
    a persona behind a proxy it has never measured asks over that proxy, which
    is the only way to get it right and costs one lookup per new proxy.
    """
    config: Dict[str, Any] = {}
    options: Dict[str, Any] = {}
    if persona.timezone:
        config["timezone"] = persona.timezone
        if persona.geolocation:
            config["geolocation:latitude"], config["geolocation:longitude"] = persona.geolocation
    elif persona.exit_ip:
        options["geoip"] = persona.exit_ip
    elif proxy and allow_lookup:
        options["geoip"] = True
    if config:
        options["config"] = config
    return options


def launch_kwargs(persona: Persona, *, headless: bool = True, proxy: Optional[str] = None,
                  humanize: bool = True, pin_cpu_cores: bool = False,
                  block_images: bool = False) -> Dict[str, Any]:
    """The keyword arguments Camoufox needs to launch this persona.

    A drawn preset is always passed back in, because drawing one is what makes
    the identity repeatable; an undrawn persona gets a preset drawn for it here,
    deterministically from its own seed, and the caller keeps the result.
    """
    kwargs: Dict[str, Any] = {
        "os": persona.os_name,
        "headless": headless,
        "humanize": humanize,
        "pin_cpu_cores": pin_cpu_cores,
    }
    languages: Sequence[str] = persona.languages
    kwargs["locale"] = list(languages) if len(languages) > 1 else persona.locale
    if block_images:
        kwargs["block_images"] = True
    settings = proxy_settings(proxy)
    if settings:
        kwargs["proxy"] = settings

    options = geo_options(persona, proxy)
    config: Dict[str, Any] = dict(options.pop("config", {}))

    if persona.preset:
        kwargs["fingerprint_preset"] = persona.preset
    if persona.ff_version:
        kwargs["ff_version"] = persona.ff_version
    kwargs.update(options)
    if config:
        kwargs["config"] = {**kwargs.get("config", {}), **config}
    # Camoufox prints one warning per launch that canvas and WebGL readback are
    # randomised, because the identity claims another GPU than this machine's and
    # its own canvas would betray that. That is the choice, not an oversight:
    # noise makes the browser look like a hardened Firefox, while the real canvas
    # would contradict the WebGL string in a way any page can read. We take the
    # hardened look, so the warning is answered once, here, instead of on the
    # user's stderr on every render.
    kwargs.setdefault("i_know_what_im_doing", True)
    return kwargs


def draw_preset(persona: Persona) -> Dict[str, Any]:
    """Draw a real device preset for ``persona`` and remember it on it.

    Returns an empty dict when Camoufox has no presets to give, which leaves
    Camoufox's own synthetic generation in charge of the identity.
    """
    if persona.preset:
        return persona.preset
    try:
        from camoufox.fingerprints import get_random_preset
    except Exception:
        persona.note("this Camoufox has no fingerprint presets; the identity is synthetic")
        return {}
    try:
        preset = get_random_preset(os=persona.os_name[:3], ff_version=str(persona.ff_version or "") or None)
    except TypeError:  # older signature without ff_version
        try:
            preset = get_random_preset(os=persona.os_name[:3])
        except Exception:
            preset = None
    except Exception:
        preset = None
    if not preset:
        persona.note("Camoufox had no preset for this os; the identity is synthetic")
        return {}
    persona.preset = dict(preset)
    return persona.preset


# -- patching the config Camoufox is about to load ---------------------------

def _chunk_size(env: Dict[str, Any], keys: List[str]) -> int:
    """Recover the launcher's chunk size so the payload re-splits identically."""
    if len(keys) > 1:
        return len(str(env[keys[0]]))
    # One chunk cannot tell us the size it was cut at, so use the launcher's own:
    # Windows environment strings are capped far lower than POSIX ones.
    return _WIN_CHUNK if os.name == "nt" else _OTHER_CHUNK


def patch_webgl(from_options: Dict[str, Any]) -> Tuple[Dict[str, Any], bool]:
    """Rewrite the WebGL strings inside a built Camoufox launch payload.

    Camoufox hands its config to the browser through ``CAMOU_CONFIG_*``
    environment variables, so a GPU string it picked cannot be changed by
    passing options in; it has to be rewritten in that payload. Returns the
    options (patched in place when the payload was understood) and whether the
    patch was applied, so a caller can carry on without it rather than fail.
    """
    env = from_options.get("env")
    if not isinstance(env, dict):
        return from_options, False
    keys = sorted((k for k in env if str(k).startswith(_CONFIG_PREFIX)),
                  key=lambda k: int(str(k).split("_")[-1]))
    if not keys:
        return from_options, False
    try:
        config = json.loads("".join(str(env[k]) for k in keys))
    except (ValueError, TypeError):
        return from_options, False
    if not isinstance(config, dict):
        return from_options, False

    applied = False
    renderer = config.get("webGl:renderer")
    if renderer is not None:
        clean = clean_renderer(renderer)
        if clean != renderer:
            config["webGl:renderer"] = clean
            applied = True
    if not applied:
        return from_options, False

    # ensure_ascii: on Windows the browser reads its environment through the
    # ANSI code page, so escaping non-ASCII characters is the safe form.
    payload = json.dumps(config, separators=(",", ":"), ensure_ascii=True)
    size = _chunk_size(env, keys)
    chunks = max(-(-len(payload) // size), 1)
    written = set()
    for index in range(chunks):
        key = f"{_CONFIG_PREFIX}{index + 1}"
        env[key] = payload[index * size:(index + 1) * size]
        written.add(key)
    for key in list(env):
        # Anything numbered past what we wrote would still be concatenated by
        # the browser, and a leftover piece of the old payload reads as a GPU
        # string we never meant to send.
        if str(key).startswith(_CONFIG_PREFIX) and str(key) not in written:
            del env[key]
    return from_options, True


# -- reading back what a live browser actually reports -----------------------

def identity_from(signals: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the persona's fields out of what a page was able to read.

    One probe answers both questions -- is this browser consistent, and what
    does it actually claim -- so the persona records the same values a detector
    would have read, not the values we meant to set.
    """
    return {
        "userAgent": str(signals.get("userAgent") or ""),
        "timezone": str(signals.get("timezone") or ""),
        "webglRenderer": str(signals.get("webglRenderer") or ""),
        "webglVendor": str(signals.get("webglVendor") or ""),
        "screen": tuple(signals.get("screen") or ())[:2],
        "devicePixelRatio": signals.get("devicePixelRatio"),
    }


def browser_version(browser: Any) -> str:
    """The Firefox version the launched browser really is."""
    try:
        return str(browser.version or "")
    except Exception:
        return ""
