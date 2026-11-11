"""``python -m wayfarer`` -- measure the disguise, and check what is installed.

``diagnose`` launches each browser backend, loads a page, and reports what a
page could read back about it, so a stealth layer stays a claim you can check
rather than one you have to trust. ``doctor`` answers the questions that cost
an hour to find: is the Camoufox binary actually fetched, which version, does
the solver key work, is the browser new enough to match the HTTP profile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .detect import Report, diagnose, load_reports, save_reports


def _format(report: Report) -> str:
    lines = [f"{report.engine}: {len(report.hard_leaks)} leak(s)"]
    for leak in report.leaks:
        mark = "!" if leak.severity == "leak" else "-"
        lines.append(f"  {mark} {leak.code}: {leak.message}")
    if not report.leaks:
        lines.append("  nothing a page could read contradicts itself")
    renderer = str((report.signals or {}).get("webglRenderer") or "")
    if renderer:
        lines.append(f"  gpu: {renderer}")
    return "\n".join(lines)


def _compare(reports: Sequence[Report], baseline_path: str) -> List[str]:
    lines: List[str] = []
    try:
        baseline = load_reports(baseline_path)
    except (OSError, ValueError) as exc:
        return [f"could not read baseline {baseline_path}: {exc}"]
    for report in reports:
        past = baseline.get(report.engine)
        if past is None:
            lines.append(f"{report.engine}: no baseline recorded")
            continue
        delta = report.compare(past)
        if not delta["new"] and not delta["gone"]:
            lines.append(f"{report.engine}: same as baseline")
        for code in delta["new"]:
            lines.append(f"{report.engine}: NEW leak {code}")
        for code in delta["gone"]:
            lines.append(f"{report.engine}: fixed {code}")
    return lines


def cmd_diagnose(args: argparse.Namespace) -> int:
    backends = [name.strip() for name in args.backends.split(",") if name.strip()]
    try:
        reports = diagnose(backends, url=args.url, headless=not args.headed, timeout=args.timeout)
    except Exception as exc:  # a missing binary is the common case; say what to do
        print(f"diagnose could not run: {exc}", file=sys.stderr)
        return 2

    for report in reports:
        print(_format(report))
        print()
    if args.json:
        save_reports(reports, args.json)
        print(f"wrote {args.json}")
    if args.baseline:
        for line in _compare(reports, args.baseline):
            print(line)
    leaks = sum(len(report.hard_leaks) for report in reports)
    return 1 if (args.fail_on_leak and leaks) else 0


def _playwright_fit(installed: str) -> str:
    """Whether Playwright is inside the range Camoufox says it can drive.

    The bound comes out of Camoufox's own metadata rather than being written
    here, so it stays true when either package moves. Getting it wrong is not a
    warning but a launch that dies inside ``new_context``, which reads like a
    broken browser instead of an unsupported combination.
    """
    try:
        from importlib.metadata import requires

        specs = [line.split(";")[0].strip() for line in (requires("camoufox") or [])
                 if line.strip().lower().startswith("playwright")]
    except Exception:
        return ""
    if not specs or installed.startswith("not installed"):
        return ""

    def number(text: str):
        return tuple(int(part) for part in text.split(".") if part.isdigit())

    have = number(installed)
    for operator, value in re.findall(r"(<=|>=|<|==)\s*([\d.]+)", specs[0]):
        want = number(value)
        outside = (have >= want) if operator == "<" else \
                  (have > want) if operator == "<=" else \
                  (have < want) if operator == ">=" else (have != want)
        if outside:
            return f"{installed} does not satisfy {specs[0]}: Camoufox will fail to open a context"
    return f"{installed} satisfies {specs[0]}"


def _doctor() -> Dict[str, str]:
    facts: Dict[str, str] = {}
    facts["wayfarer"] = __version__
    facts["python"] = sys.version.split()[0]
    try:
        import curl_cffi

        from curl_cffi.requests.impersonate import DEFAULT_CHROME, DEFAULT_FIREFOX

        facts["curl_cffi"] = f"{curl_cffi.__version__} (chrome {DEFAULT_CHROME}, firefox {DEFAULT_FIREFOX})"
    except Exception as exc:
        facts["curl_cffi"] = f"not usable: {exc}"
    try:
        from importlib.metadata import version

        facts["playwright"] = version("playwright")
    except Exception:
        facts["playwright"] = "not installed (pip install 'wayfarer[browser]')"
    from . import stealth

    facts["camoufox"] = stealth.camoufox_version() or "not installed (pip install 'wayfarer[stealth]')"
    try:
        from camoufox.pkgman import camoufox_path  # type: ignore

        # download_if_missing=False: the default fetches ~200 MB, and a command
        # whose job is to report the state of the machine should not change it.
        path = str(camoufox_path(download_if_missing=False))
        facts["camoufox browser"] = f"{path} " \
                                    f"({'present' if os.path.isdir(path) else 'missing: run python -m camoufox fetch'})"
    except Exception as exc:
        facts["camoufox browser"] = f"could not be located: {str(exc).splitlines()[0]}"
    note = _playwright_fit(facts.get("playwright", ""))
    if note:
        facts["playwright for camoufox"] = note
    for name, env in (("CapMonster", "CAPMONSTER_API_KEY"), ("2Captcha", "TWOCAPTCHA_API_KEY")):
        facts[f"{name} key"] = "set" if os.environ.get(env) else f"unset (set {env})"
    try:
        from .profiles import PROFILES

        from .stealth import webgl_pairs

        pairs = webgl_pairs("win")
        facts["camoufox GPU records"] = (
            f"{len(pairs)} known, {sum(', or similar' in r for _, r in pairs)} carrying the "
            "hedge wayfarer rewrites"
        )
        facts["profiles"] = ", ".join(sorted(PROFILES))
    except Exception:
        pass
    return facts


def cmd_doctor(args: argparse.Namespace) -> int:
    facts = _doctor()
    width = max(len(name) for name in facts)
    for name, value in facts.items():
        print(f"{name.ljust(width)}  {value}")
    if args.balance:
        from .solvers import solver_from_env

        solver = solver_from_env()
        if solver is None:
            print("\nno solver key in the environment, so nothing to check")
        else:
            try:
                print(f"\n{solver.provider if hasattr(solver, 'provider') else solver}: "
                      f"balance {solver.balance()}")
            except Exception as exc:
                print(f"\nthe key did not answer: {exc}")
                return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m wayfarer")
    commands = parser.add_subparsers(dest="command", required=True)

    detect = commands.add_parser("diagnose", help="report what each browser backend leaks")
    detect.add_argument("--backends", default="camoufox",
                        help="comma separated: playwright, camoufox")
    detect.add_argument("--url", default="about:blank")
    detect.add_argument("--headed", action="store_true", help="show the browser window")
    detect.add_argument("--timeout", type=float, default=60.0)
    detect.add_argument("--json", help="write the reports here")
    detect.add_argument("--baseline", help="compare against reports saved here")
    detect.add_argument("--fail-on-leak", action="store_true", help="exit 1 on any leak")
    detect.set_defaults(func=cmd_diagnose)

    fix = commands.add_parser("doctor", help="check what is installed and configured")
    fix.add_argument("--balance", action="store_true", help="also ask the solver for its balance")
    fix.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    result: Any = args.func(args)
    return int(result or 0)


if __name__ == "__main__":
    raise SystemExit(main())
