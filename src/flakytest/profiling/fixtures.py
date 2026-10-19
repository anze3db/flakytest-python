"""Exact fixture setup times, added up per fixture: calls, total, max, a histogram for the median and p99, and each
process's first call (which pays for warming up whatever the fixture starts)."""

import math
from collections import Counter
from typing import Any

BUCKETS_PER_DECADE = 20
# A test keeps its own entry for a fixture this slow, or of a wider scope than function; faster ones only count here
SLOW_FIXTURE = 0.005


def bucket(duration: float) -> int:
    return math.floor(math.log10(duration) * BUCKETS_PER_DECADE) if duration > 0 else -10 * BUCKETS_PER_DECADE


def bucket_upper(index: int) -> float:
    return 10 ** ((index + 1) / BUCKETS_PER_DECADE)


class FixtureStats:
    def __init__(self) -> None:
        self.entries: dict[tuple[str, str], dict[str, Any]] = {}

    def add(self, name: str, scope: str, duration: float, nodeid: str | None) -> None:
        entry = self.entries.get((name, scope))
        if entry is None:
            entry = self.entries[(name, scope)] = {
                "name": name,
                "scope": scope,
                "calls": 0,
                "total": 0.0,
                "max": 0.0,
                "histogram": Counter(),
                "first": {"duration": duration, "nodeid": nodeid},
            }
        entry["calls"] += 1
        entry["total"] += duration
        entry["max"] = max(entry["max"], duration)
        entry["histogram"][bucket(duration)] += 1

    def as_list(self) -> list[dict[str, Any]]:
        return [
            entry | {"histogram": [[k, v] for k, v in entry["histogram"].items()]} for entry in self.entries.values()
        ]


def quantile(histogram: dict[int, int], q: float) -> float:
    """The q-quantile from a histogram, as the upper edge of its bucket (within ~12%)."""
    total = sum(histogram.values())
    if not total:
        return 0.0
    seen = 0
    for index in sorted(histogram):
        seen += histogram[index]
        if seen >= q * total:
            return bucket_upper(index)
    return bucket_upper(max(histogram))


def merge(processes: list[list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Every process's stats added up, with the median, p99 and calls over a second, slowest in total first."""
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for stats in processes:
        for entry in stats:
            key = (entry["name"], entry["scope"])
            target = merged.setdefault(
                key,
                {"name": entry["name"], "scope": entry["scope"], "calls": 0, "total": 0.0, "max": 0.0, "_h": Counter()},
            )
            target["calls"] += entry["calls"]
            target["total"] += entry["total"]
            target["max"] = max(target["max"], entry["max"])
            for index, count in entry["histogram"]:
                target["_h"][index] += count
    result = []
    for entry in sorted(merged.values(), key=lambda e: -e["total"]):
        histogram = entry.pop("_h")
        entry["total"] = round(entry["total"], 6)
        entry["max"] = round(entry["max"], 6)
        entry["mean"] = round(entry["total"] / entry["calls"], 6) if entry["calls"] else 0.0
        entry["median"] = round(min(quantile(histogram, 0.5), entry["max"]), 6)
        entry["p99"] = round(min(quantile(histogram, 0.99), entry["max"]), 6)
        entry["over_1s"] = sum(count for index, count in histogram.items() if index >= 0)
        result.append(entry)
    return result


def median(entry: dict[str, Any]) -> float:
    histogram = Counter(dict(entry["histogram"]))
    return min(quantile(histogram, 0.5), entry["max"])
