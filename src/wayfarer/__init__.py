"""wayfarer: visit websites the way a real browser would, without a browser."""

from .browser import NetworkCall, RenderedPage
from .challenges import Challenge, detect_challenge
from .detect import Leak, Report, diagnose, lint, report
from .errors import (
    ChallengeError,
    RenderError,
    RobotsDisallowed,
    WayfarerError,
)
from .pacing import HostPacer, RetryPolicy
from .personas import Persona
from .profiles import PROFILES, Profile
from .proxies import ProxyPool
from .session import AsyncSession, Session
from .solvers import CapMonster, Solved, SolverChain, SolverError, TwoCaptcha
from .stealth import CamoufoxMissing

__version__ = "0.2.0"

__all__ = [
    "AsyncSession",
    "CamoufoxMissing",
    "CapMonster",
    "Challenge",
    "ChallengeError",
    "HostPacer",
    "Leak",
    "NetworkCall",
    "PROFILES",
    "Persona",
    "Profile",
    "ProxyPool",
    "RenderError",
    "RenderedPage",
    "Report",
    "RetryPolicy",
    "RobotsDisallowed",
    "Session",
    "Solved",
    "SolverChain",
    "SolverError",
    "TwoCaptcha",
    "WayfarerError",
    "detect_challenge",
    "diagnose",
    "lint",
    "report",
]
