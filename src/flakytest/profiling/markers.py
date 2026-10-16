"""Marker frames: every test phase runs inside a generated function named after the test.

A sampling profiler records the marker in every stack it captures while the phase runs, so samples can
be attributed to a test and phase without synchronising clocks, and the same way for every profiler.
"""

import hashlib
import types
from collections.abc import Callable
from typing import Any

from _pytest.runner import CallInfo

MARKER_FILENAME = "<flakytest>"
MARKER_PREFIX = "__flakytest_"
PHASES = ("setup", "call", "teardown")


def marker_key(nodeid: str) -> str:
    return hashlib.sha1(nodeid.encode(), usedforsecurity=False).hexdigest()[:12]


def _run(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


def marker_name(phase: str, key: str | None = None) -> str:
    return f"{MARKER_PREFIX}{phase}_{key}__" if key else f"{MARKER_PREFIX}{phase}__"


def make_marker(phase: str, key: str | None = None) -> Callable[..., Any]:
    """A function that calls fn(*args, **kwargs) from a frame named after the phase and test."""
    name = marker_name(phase, key)
    code = _run.__code__.replace(co_name=name, co_qualname=name, co_filename=MARKER_FILENAME)
    return types.FunctionType(code, _run.__globals__, name)


def parse_marker(filename: str, funcname: str) -> tuple[str, str | None] | None:
    """(phase, key) for a marker frame, None for any other frame."""
    if filename != MARKER_FILENAME or not funcname.startswith(MARKER_PREFIX) or not funcname.endswith("__"):
        return None
    phase, _, key = funcname[len(MARKER_PREFIX) : -2].partition("_")
    return phase, key or None


class PhaseMarkers:
    """Patches pytest's CallInfo.from_call so setup/call/teardown and collection run inside marker frames."""

    def __init__(self) -> None:
        self.current_key: str | None = None
        self._original: Any = None

    def install(self) -> None:
        if self._original is not None:
            return
        original = self._original = CallInfo.from_call.__func__  # type: ignore[attr-defined]
        markers = self
        collect_marker = make_marker("collect")

        def from_call(cls, func, when, reraise=None):
            if when in PHASES and markers.current_key:
                return make_marker(when, markers.current_key)(original, cls, func, when, reraise)
            if when == "collect":
                return collect_marker(original, cls, func, when, reraise)
            return original(cls, func, when, reraise)

        CallInfo.from_call = classmethod(from_call)  # type: ignore[method-assign, assignment]

    def uninstall(self) -> None:
        if self._original is not None:
            CallInfo.from_call = classmethod(self._original)  # type: ignore[method-assign, assignment]
            self._original = None
