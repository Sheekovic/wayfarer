"""Solving a CAPTCHA you have already been served.

Solving is not stealth: it is a paid round trip to a service that either runs
the challenge in a real browser or hands it to a person. Both take 10-40
seconds, and the honest number to plan around is roughly $0.60-$2.50 per
thousand, which is why wayfarer treats it as the last rung of the ladder and
records the cost and the wait in the trail it shows you when it gives up.

Both supported services are the ZennoLab-family ``createTask`` / ``getTaskResult``
API, so one loop drives both and only the task names differ. The task table
below is the thing most likely to drift as providers rename task types, so it
lives in one place, and a call can override it with ``task_type=``.

Verified against provider documentation; not exercised against a live account
in this release, since solving needs a key (``CAPMONSTER_API_KEY`` or
``TWOCAPTCHA_API_KEY``). ``solver.balance()`` is the quickest way to check a
key works before blaming the target site.
"""

from __future__ import annotations

import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit

from .errors import WayfarerError

#: What a page has to be shown, by the name wayfarer's detector uses.
KINDS = ("turnstile", "cf_clearance", "recaptcha_v2", "recaptcha_v3", "hcaptcha")

#: Field a page reads a solved token back from, by kind.
RESPONSE_FIELDS = {
    "turnstile": "cf-turnstile-response",
    "cf_clearance": "",           # a cookie, not a field
    "recaptcha_v2": "g-recaptcha-response",
    "recaptcha_v3": "g-recaptcha-response",
    "hcaptcha": "g-recaptcha-response",
}

#: Rough price per 1000, for the trail. Published rates move; these are for
#: deciding whether to pay at all, not for reconciling an invoice.
PRICE_PER_1K = {
    "recaptcha_v2": 0.60,
    "recaptcha_v3": 1.20,
    "turnstile": 1.00,
    "hcaptcha": 1.00,
    "cf_clearance": 2.50,
}

Transport = Callable[[str, Mapping[str, str], Any], Dict[str, Any]]


class SolverError(WayfarerError):
    """The solver refused, could not be reached, or gave up on this challenge."""

    def __init__(self, provider: str, code: str = "", message: str = "") -> None:
        self.provider = provider
        self.code = code
        super().__init__(f"{provider}: {code or 'error'} {message}".strip())


def http_get(url: str, *, timeout: float = 30.0) -> Dict[str, Any]:
    """GET and read JSON. 2Captcha reads its balance this way."""
    import json

    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SolverError(url, message=f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SolverError(url, message=str(exc)) from exc


def http_transport(url: str, headers: Mapping[str, str], body: Any, *, timeout: float = 30.0) -> Dict[str, Any]:
    """POST JSON and read JSON back. Split out so tests can drive the loop."""
    import json

    data = json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={**dict(headers), "content-type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise SolverError(url, message=f"HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise SolverError(url, message=str(exc)) from exc


def proxy_fields(proxy: Optional[str]) -> Dict[str, str]:
    """The four proxy fields a solver task takes, from a proxy URL."""
    if not proxy:
        return {}
    parts = urlsplit(proxy)
    if not parts.hostname:
        raise ValueError(f"not a proxy URL: {proxy!r}")
    fields = {
        "proxyType": (parts.scheme or "http").replace("socks5h", "socks5"),
        "proxyAddress": parts.hostname,
        "proxyPort": str(parts.port or 80),
    }
    if parts.username:
        from urllib.parse import unquote

        fields["proxyLogin"] = unquote(parts.username)
        fields["proxyPassword"] = unquote(parts.password or "")
    return fields


@dataclass(frozen=True)
class Solved:
    """A token, or a cookie, plus what getting it cost."""

    kind: str
    token: str = ""
    cookies: Dict[str, str] = field(default_factory=dict)
    provider: str = ""
    task_id: str = ""
    seconds: float = 0.0
    usd: float = 0.0

    def __bool__(self) -> bool:
        return bool(self.token or self.cookies)


class _CloudSolver:
    """The createTask / getTaskResult loop both providers speak."""

    provider = "solver"
    endpoint = ""
    key_env = ""
    #: kind -> task type name this provider answers that kind with.
    task_types: Mapping[str, str] = {}
    #: Kinds that need the challenge page's HTML, base64 encoded, and a proxy.
    page_types: Tuple[str, ...] = ("cf_clearance",)
    #: True when the provider reads the balance over GET ?key= instead of POST.
    balance_is_get = False

    def __init__(self, api_key: Optional[str] = None, *, transport: Optional[Transport] = None,
                 getter: Optional[Callable[[str], Dict[str, Any]]] = None,
                 timeout: float = 180.0, poll: float = 2.0) -> None:
        self.api_key = api_key or os.environ.get(self.key_env, "")
        if not self.api_key:
            raise SolverError(self.provider, message=f"no API key: set {self.key_env}")
        self._post = transport or http_transport
        self._get = getter or http_get
        self.timeout = float(timeout)
        self.poll = float(poll)

    # -- API ------------------------------------------------------------

    def _call(self, path: str, body: Dict[str, Any]) -> Dict[str, Any]:
        response = self._post(f"{self.endpoint}/{path}", {"user-agent": "wayfarer"}, body)
        code = str(response.get("errorDescription") or response.get("error_code") or "")
        if int(response.get("errorId", 0) or 0) != 0:
            raise SolverError(self.provider, code, str(response.get("errorDescription", "")))
        if code.startswith("ERROR"):
            raise SolverError(self.provider, code)
        return response

    def balance(self) -> float:
        """Account balance in dollars, the quickest check that a key is live."""
        if self.balance_is_get:
            response = self._get(f"{self.endpoint}/getBalance?key={self.api_key}")
        else:
            response = self._call("getBalance", {"clientKey": self.api_key})
        for key in ("balance", "usd"):
            if key in response:
                return float(response[key])
        raise SolverError(self.provider, message="the balance response was not understood")

    def create_task(self, kind: str, *, sitekey: str, url: str, proxy: Optional[str] = None,
                    user_agent: str = "", page_html: str = "", action: str = "",
                    min_score: Optional[float] = None, task_type: Optional[str] = None,
                    extra: Optional[Mapping[str, Any]] = None) -> str:
        """Send one task and return its id. ``cf_clearance`` needs a proxy."""
        name = task_type or self.task_types.get(kind)
        if not name:
            raise SolverError(self.provider, message=f"no task type for {kind!r}; pass task_type=")
        task: Dict[str, Any] = {"type": name, "websiteURL": url}
        if sitekey:
            task["websiteKey"] = sitekey
        if action:
            task["captchaExtraApiParams"] = {"action": action, **({"minScore": min_score} if min_score else {})}
        if kind in self.page_types:
            if not proxy:
                raise SolverError(self.provider, message=f"{kind} requires the same proxy the browser used")
            import base64

            if page_html:
                task["htmlPageBase64"] = base64.b64encode(page_html.encode("utf-8")).decode("ascii")
            if user_agent:
                task["userAgent"] = user_agent
        if proxy:
            task.update(proxy_fields(proxy))
        if extra:
            task.update(extra)
        response = self._call("createTask", {"clientKey": self.api_key, "task": task})
        task_id = response.get("taskId") or response.get("requestId")
        if not task_id:
            raise SolverError(self.provider, message="createTask returned no task id")
        return str(task_id)

    def wait_for_result(self, task_id: str, kind: str, *, deadline: Optional[float] = None) -> Solved:
        """Poll until the task is ready, the queue errors clear, or time runs out."""
        started = time.monotonic()
        limit = started + self.timeout if deadline is None else deadline
        while True:
            response = self._call("getTaskResult", {"clientKey": self.api_key, "taskId": task_id})
            if response.get("status") == "ready":
                solution = dict(response.get("solution") or {})
                token = str(solution.get("token") or solution.get("gRecaptchaResponse")
                            or solution.get("solution") or solution.get("cookie") or solution.get("cf_clearance") or "")
                cookies = {
                    name: str(value)
                    for name, value in solution.items()
                    if name in ("cf_clearance", "cookie", "data") and value
                }
                if kind == "cf_clearance" and not cookies and token:
                    cookies = {"cf_clearance": token}
                price = PRICE_PER_1K.get(kind, 0.0)
                return Solved(
                    kind=kind, token=token, cookies=cookies, provider=self.provider,
                    task_id=task_id, seconds=round(time.monotonic() - started, 2),
                    usd=round(price / 1000, 5),
                )
            if time.monotonic() > limit:
                raise SolverError(self.provider, "ERROR_TIMEOUT",
                                  f"{kind} was still unsolved after {self.timeout:.0f}s")
            time.sleep(self.poll)

    def solve(self, kind: str, **kwargs: Any) -> Solved:
        """Create a task and wait for it, in one call."""
        task_id = self.create_task(kind, **kwargs)
        return self.wait_for_result(task_id, kind)

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.provider} key=***>"


class CapMonster(_CloudSolver):
    """CapMonster Cloud: AI solver, cheaper and faster than a human service.

    Try it first: Turnstile, reCAPTCHA and the ``cf_clearance`` cookie are all
    covered, and paying only for solved tasks keeps a failed attempt free.
    """

    provider = "CapMonster Cloud"
    endpoint = "https://api.capmonster.cloud"
    key_env = "CAPMONSTER_API_KEY"
    task_types = {
        "turnstile": "TurnstileTaskProxyLess",
        "cf_clearance": "AntiCloudflareTurnstileTaskProxyLess",
        "recaptcha_v2": "RecaptchaV2TaskProxyLess",
        "recaptcha_v3": "RecaptchaV3TaskProxyLess",
        "hcaptcha": "HCaptchaTaskProxyLess",
    }


class TwoCaptcha(_CloudSolver):
    """2Captcha: human-backed, slower and dearer, better on odd challenge types.

    Fall back to it when CapMonster returns ERROR_CAPTCHA_UNSOLVABLE twice;
    it no longer sells hCaptcha, so keep CapMonster for that.
    """

    provider = "2Captcha"
    endpoint = "https://api.2captcha.com"
    key_env = "TWOCAPTCHA_API_KEY"
    balance_is_get = True
    task_types = {
        "turnstile": "AntiTurnstileTaskProxyLess",
        "cf_clearance": "AntiCloudflareTaskProxyLess",
        "recaptcha_v2": "RecaptchaV2TaskProxyLess",
        "recaptcha_v3": "RecaptchaV3TaskProxyLess",
    }


class SolverChain:
    """Solvers in order of preference; the first one that answers wins."""

    def __init__(self, solvers: Sequence[_CloudSolver]) -> None:
        if not solvers:
            raise ValueError("SolverChain needs at least one solver")
        self.solvers = list(solvers)

    def solve(self, kind: str, **kwargs: Any) -> Solved:
        failures: List[str] = []
        for solver in self.solvers:
            try:
                return solver.solve(kind, **kwargs)
            except SolverError as exc:
                failures.append(str(exc))
        raise SolverError(" / ".join(solver.provider for solver in self.solvers),
                          message="; ".join(failures))

    def __repr__(self) -> str:
        return "<SolverChain " + " then ".join(solver.provider for solver in self.solvers) + ">"


def solver_from_env(*, prefer: str = "capmonster") -> Optional[Any]:
    """Build the solver set the environment has keys for. None when it has none.

    CapMonster first by default: it is cheaper, faster, and the only one of the
    two that will hand back a ``cf_clearance`` cookie.
    """
    order = [CapMonster, TwoCaptcha] if prefer != "2captcha" else [TwoCaptcha, CapMonster]
    available = [cls() for cls in order if os.environ.get(cls.key_env)]
    if not available:
        return None
    return available[0] if len(available) == 1 else SolverChain(available)


# -- handing a solved token back to the page ---------------------------------

#: Where a page looks for the token its widget was supposed to produce.
_SET_TOKEN_JS = """
(args) => {
  const field = args.field, token = args.token;
  let el = document.querySelector('[name="' + field + '"]') || document.getElementById(field);
  if (!el) {
    el = document.createElement('textarea');
    el.name = field; el.id = field;
    el.style.display = 'none';
    (document.body || document.documentElement).appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  return true;
}
"""

_SUBMIT_SELECTOR = "form button[type=submit], form input[type=submit], button[type=submit]"


def apply_token(page: Any, kind: str, token: str, *, submit: bool = True) -> bool:
    """Give a live page its solved token, the way the widget's own script would.

    Returns False when the page has no field for the token at all, which means
    the challenge on it is not the kind we asked the solver about.
    """
    field = RESPONSE_FIELDS.get(kind, "")
    if not field or not token:
        return False
    try:
        placed = bool(page.evaluate(_SET_TOKEN_JS, {"field": field, "token": token}))
    except Exception:
        return False
    if placed and submit:
        try:
            page.click(_SUBMIT_SELECTOR, timeout=5000)
        except Exception:
            pass  # a token-only page has no submit button, and that is fine
    return placed


def apply_cookies(page: Any, cookies: Mapping[str, str], domain: str) -> bool:
    """Write a solved cookie (``cf_clearance``) into the browser context."""
    if not cookies or not domain:
        return False
    payload = [{"name": name, "value": value, "domain": domain, "path": "/"}
               for name, value in cookies.items()]
    try:
        page.context.add_cookies(payload)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False


async def aapply_token(page: Any, kind: str, token: str, *, submit: bool = True) -> bool:
    """Awaitable twin of :func:`apply_token`."""
    field = RESPONSE_FIELDS.get(kind, "")
    if not field or not token:
        return False
    try:
        placed = bool(await page.evaluate(_SET_TOKEN_JS, {"field": field, "token": token}))
    except Exception:
        return False
    if placed and submit:
        try:
            await page.click(_SUBMIT_SELECTOR, timeout=5000)
        except Exception:
            pass
    return placed


async def aapply_cookies(page: Any, cookies: Mapping[str, str], domain: str) -> bool:
    """Awaitable twin of :func:`apply_cookies`."""
    if not cookies or not domain:
        return False
    payload = [{"name": name, "value": value, "domain": domain, "path": "/"}
               for name, value in cookies.items()]
    try:
        await page.context.add_cookies(payload)  # type: ignore[attr-defined]
        return True
    except Exception:
        return False
