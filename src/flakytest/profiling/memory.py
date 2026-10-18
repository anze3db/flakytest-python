"""Per-test memory: the test process's resident memory (RSS) when each test starts and ends, and its peak while it
runs.

On Linux the kernel's peak counter (VmHWM) is reset before every test by writing 5 to /proc/self/clear_refs, so each
test gets its own peak. Private memory (RssAnon, which excludes the interpreter and shared libraries every process
maps) is recorded too, so pytest-xdist workers can be added up. Elsewhere the peak counter can't be reset: a test's
peak is only known when it raised the process's peak, and with psutil the start and end RSS are known too.
"""

import contextlib
import re
import sys
import time
from pathlib import Path
from typing import Any

try:
    import resource
except ImportError:  # Windows
    resource = None  # type: ignore[assignment]

try:
    import psutil  # type: ignore[import-untyped, unused-ignore]
except ImportError:
    psutil = None

STATUS = Path("/proc/self/status")
CLEAR_REFS = Path("/proc/self/clear_refs")
RESET_PEAK = "5"  # resets VmHWM to the current RSS (Linux 4.0+)
FIELDS = re.compile(r"^(VmRSS|VmHWM|RssAnon):\s+(\d+) kB", re.MULTILINE)
KB = 1024
MB = 1024**2
GB = 1024**3
GROWTH_MIN = 256 * MB  # a process growing less than this isn't worth a warning
GROWTH_SHARE = 0.5  # ... nor growing by less than half of what it started with


class MemoryMeter:
    """Measures each test in the process it runs in. `source` says how: "linux" (a peak per test), "psutil" or
    "maxrss" (the peak only when a test raised it; maxrss doesn't know the current RSS), or None (nothing)."""

    def __init__(self) -> None:
        self.source = self._detect()
        self.max_peak = 0

    def _detect(self) -> str | None:
        if sys.platform.startswith("linux") and STATUS.exists():
            try:
                CLEAR_REFS.write_text(RESET_PEAK)
            except OSError:
                return "psutil" if psutil else "maxrss"
            return "linux"
        if psutil is not None:
            return "psutil"
        return "maxrss" if resource is not None else None

    @property
    def peak_per_test(self) -> bool:
        return self.source == "linux"

    def start(self) -> dict[str, Any] | None:
        """Call before the test's setup."""
        if self.source is None:
            return None
        if self.source == "linux":
            with contextlib.suppress(OSError):
                CLEAR_REFS.write_text(RESET_PEAK)
        reading = self._read()
        self.max_peak = max(self.max_peak, reading.get("peak") or 0)
        return reading | {"at": time.time()}

    def stop(self, started: dict[str, Any] | None) -> dict[str, Any] | None:
        """Call after the test's teardown: what the test used, in bytes."""
        if started is None:
            return None
        reading = self._read()
        result: dict[str, Any] = {"at": started["at"]}
        start, end, peak = started.get("rss"), reading.get("rss"), reading.get("peak")
        if self.source != "linux":
            # The peak counter only goes up: it tells about this test only if the test raised it
            peak = peak if peak and peak > self.max_peak else None
            if reading.get("peak"):
                self.max_peak = max(self.max_peak, reading["peak"])
            if peak is not None and start is None:
                start = started.get("peak")  # maxrss: the earlier peak is the best known starting point
        if start is not None:
            result["start"] = start
        if end is not None:
            result["end"] = end
        if peak is not None:
            result["peak"] = max(peak, start or 0, end or 0)
            if start is not None:
                result["peak_increase"] = result["peak"] - start
        if start is not None and end is not None:
            result["growth"] = end - start
        if started.get("private") is not None and reading.get("private") is not None:
            result["private_start"] = started["private"]
            result["private_end"] = reading["private"]
        return result

    def _read(self) -> dict[str, Any]:
        if self.source == "linux":
            try:
                values = {name: int(kb) * KB for name, kb in FIELDS.findall(STATUS.read_text())}
            except OSError:
                return {}
            return {"rss": values.get("VmRSS"), "peak": values.get("VmHWM"), "private": values.get("RssAnon")}
        if self.source == "psutil":
            info = psutil.Process().memory_info()
            peak = getattr(info, "peak_wset", None) or maxrss()
            return {"rss": info.rss, "peak": peak}
        return {"peak": maxrss()}


def maxrss() -> int | None:
    if resource is None:
        return None
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if sys.platform == "darwin" else value * KB  # bytes on macOS, kilobytes elsewhere


def test_memory(memory: dict[str, Any] | None, started: float) -> dict[str, Any] | None:
    """A test's memory for the profile document: `at` becomes seconds since the session started."""
    if not memory:
        return None
    return {key: value for key, value in memory.items() if key != "at"} | {"at": round(memory["at"] - started, 3)}


def private_peak(memory: dict[str, Any]) -> int | None:
    """The test's peak private memory: its peak RSS less the shared part (RSS - private) it started with."""
    peak = memory.get("peak")
    if peak is None:
        return memory.get("private_end", memory.get("end"))
    if memory.get("private_start") is not None and memory.get("start") is not None:
        return peak - (memory["start"] - memory["private_start"])
    return peak


def summarize(tests: list[dict[str, Any]], source: str | None) -> dict[str, Any] | None:
    """Per process (pytest-xdist worker) peaks and growth, and every process added up over time: the most memory the
    test run needed at once, and which tests were running then."""
    measured = [t for t in tests if t.get("memory")]
    if not measured:
        return None
    private = all(t["memory"].get("private_start") is not None for t in measured)
    by_worker: dict[str, list[dict[str, Any]]] = {}
    for test in sorted(measured, key=lambda t: t["memory"]["at"]):
        by_worker.setdefault(test.get("worker") or "", []).append(test)

    processes = []
    events: list[tuple[float, int, str, int, str | None]] = []  # (time, order, worker, bytes, running test)
    for worker, items in sorted(by_worker.items()):
        first, last = items[0]["memory"], items[-1]["memory"]
        peaks = [t["memory"]["peak"] for t in items if t["memory"].get("peak") is not None]
        begin = first.get("private_start") if private else first.get("start")
        finish = last.get("private_end") if private else last.get("end")
        processes.append(
            {
                "worker": worker or None,
                "tests": len(items),
                "peak": max(peaks) if peaks else None,
                "start": begin,
                "end": finish,
                "growth": finish - begin if begin is not None and finish is not None else None,
            }
        )
        for test in items:
            memory = test["memory"]
            during = private_peak(memory) if private else memory.get("peak", memory.get("end"))
            after = memory.get("private_end") if private else memory.get("end")
            if during is not None:
                events.append((memory["at"], 1, worker, during, test["nodeid"]))
            if after is not None:
                events.append((memory["at"] + test["duration"], 0, worker, after, None))

    current: dict[str, int] = {}
    running: dict[str, str | None] = {}
    total = 0
    machine_peak, machine_peak_at, machine_peak_tests = 0, 0.0, []
    points: list[tuple[float, int]] = []
    for at, _order, worker, value, nodeid in sorted(events):
        total += value - current.get(worker, 0)
        current[worker] = value
        running[worker] = nodeid
        points.append((at, total))
        if total > machine_peak:
            machine_peak, machine_peak_at = total, at
            machine_peak_tests = sorted(test for test in running.values() if test)

    peaks = [p["peak"] for p in processes if p["peak"] is not None]
    return {
        "source": source,
        "peak_per_test": source == "linux",
        "private": private,
        "peak": max(peaks) if peaks else None,
        "machine_peak": machine_peak or None,
        "machine_peak_at": round(machine_peak_at, 3),
        "machine_peak_tests": machine_peak_tests[:20],
        "processes": processes,
        "timeline": downsample(points),
    }


def downsample(points: list[tuple[float, int]], buckets: int = 120) -> list[list[float]]:
    """At most `buckets` points, keeping each bucket's highest value so peaks survive."""
    if len(points) <= buckets:
        return [[round(at, 3), value] for at, value in points]
    start, end = points[0][0], points[-1][0]
    width = (end - start) / buckets or 1.0
    kept: dict[int, tuple[float, int]] = {}
    for at, value in points:
        bucket = min(int((at - start) / width), buckets - 1)
        if bucket not in kept or value > kept[bucket][1]:
            kept[bucket] = (at, value)
    return [[round(at, 3), value] for _bucket, (at, value) in sorted(kept.items())]


def growth_warning(summary: dict[str, Any] | None, tests: list[dict[str, Any]]) -> dict[str, Any] | None:
    """A process whose memory kept growing over the run: something holds on to what tests create."""
    if not summary:
        return None
    growing = [
        p
        for p in summary["processes"]
        if p["growth"] is not None and p["growth"] >= max(GROWTH_MIN, GROWTH_SHARE * (p["start"] or 0))
    ]
    if not growing:
        return None
    worst = max(growing, key=lambda p: p["growth"])
    where = f" in {worst['worker']}" if worst["worker"] else ""
    suspects = sorted(
        (t for t in tests if (t.get("worker") or None) == worst["worker"] and (t.get("memory") or {}).get("growth")),
        key=lambda t: -t["memory"]["growth"],
    )[:5]
    return {
        "code": "memory_growth",
        "message": f"Memory grew by {mb(worst['growth'])}{where} over {worst['tests']} tests and wasn't freed. "
        "Something keeps what tests create: module-level caches or lists, lru_cache, signal receivers, or "
        "Django's connection.queries with DEBUG=True. The tests it grew most in are listed.",
        "tests": [t["nodeid"] for t in suspects],
        "growth": worst["growth"],
    }


def mb(value: float) -> str:
    if abs(value) < MB:
        return f"{value / KB:.0f} KB"
    return f"{value / MB:.0f} MB" if abs(value) < GB else f"{value / GB:.1f} GB"
