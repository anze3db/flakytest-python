"""Split a profile into parts for upload, so a large suite never needs one huge request (or a server worker holding
the whole suite in memory).

Every part is a valid profile document: the run and summary, some of the tests, and only the frames, stack nodes,
categories and source lines those tests use (renumbered). The first part also carries the session-wide insights,
warnings and fixtures. "part": {"index", "count"} tells flakytest.dev how to put them back together.
"""

import json
from typing import Any

PART_TESTS = 2000
PART_BYTES = 16 * 1024 * 1024  # uncompressed JSON per part; flakytest.dev accepts up to 50 MB
SESSION_KEYS = ("insights", "warnings", "fixtures")
TABLE_KEYS = ("frames", "stacks", "stack_categories", "sources")


def split_profile(
    profile: dict[str, Any], max_tests: int = PART_TESTS, max_bytes: int = PART_BYTES
) -> list[dict[str, Any]]:
    tests = profile["tests"]
    if len(tests) <= max_tests and size(profile) <= max_bytes:
        return [profile | {"part": {"index": 0, "count": 1}}]
    groups = [tests[i : i + max_tests] for i in range(0, len(tests), max_tests)]
    parts = [part for group in groups for part in fit(profile, group, max_bytes)]
    # The first part carries the insights, whose locations point at frames too
    parts[0] = subset(profile, [test for test in tests if test["nodeid"] in nodeids(parts[0])], with_session=True)
    for index in range(1, len(parts)):
        parts[index] = {key: value for key, value in parts[index].items() if key not in SESSION_KEYS}
    return [part | {"part": {"index": index, "count": len(parts)}} for index, part in enumerate(parts)]


def nodeids(part: dict[str, Any]) -> set[str]:
    return {test["nodeid"] for test in part["tests"]}


def size(document: dict[str, Any]) -> int:
    return len(json.dumps(document, separators=(",", ":")))


def fit(profile: dict[str, Any], tests: list[dict[str, Any]], max_bytes: int) -> list[dict[str, Any]]:
    """One part with these tests, halved until each part is under max_bytes (a single test always fits)."""
    part = subset(profile, tests)
    if len(tests) > 1 and size(part) > max_bytes:
        middle = len(tests) // 2
        return fit(profile, tests[:middle], max_bytes) + fit(profile, tests[middle:], max_bytes)
    return [part]


def subset(profile: dict[str, Any], tests: list[dict[str, Any]], *, with_session: bool = False) -> dict[str, Any]:
    """The profile with only these tests and the frames and stacks they (and, with_session, the insights) refer
    to."""
    frames = profile.get("frames", [])
    stacks = profile.get("stacks", [])
    frame_ids: dict[int, int] = {}
    node_ids: dict[int, int] = {}
    new_frames: list[Any] = []
    new_stacks: list[list[int]] = []

    def frame_id(old: int | None) -> int | None:
        if old is None:
            return None
        if old not in frame_ids:
            frame_ids[old] = len(new_frames)
            new_frames.append(frames[old])
        return frame_ids[old]

    def node_id(old: int) -> int:
        path = []
        node = old
        while node >= 0 and node not in node_ids:
            path.append(node)
            node = stacks[node][0]
        for node in reversed(path):
            parent, frame = stacks[node]
            node_ids[node] = len(new_stacks)
            new_stacks.append([node_ids[parent] if parent >= 0 else -1, frame_id(frame)])  # type: ignore[list-item]
        return node_ids[old]

    new_tests = []
    for test in tests:
        entry = dict(test)
        entry["stacks"] = {
            phase: [[node_id(node), count] for node, count in pairs] for phase, pairs in test["stacks"].items()
        }
        entry["segments"] = [segment | {"frame": frame_id(segment["frame"])} for segment in test["segments"]]
        entry["hot_lines"] = [line | {"frame": frame_id(line["frame"])} for line in test["hot_lines"]]
        new_tests.append(entry)

    categories = profile.get("stack_categories", {})
    sources = profile.get("sources", {})
    part = {key: value for key, value in profile.items() if key not in (*TABLE_KEYS, "tests")}
    if with_session:
        part["insights"] = [
            insight
            | {"locations": [location | {"frame": frame_id(location["frame"])} for location in insight["locations"]]}
            for insight in profile.get("insights", [])
        ]
    return part | {
        "frames": new_frames,
        "stacks": new_stacks,
        "stack_categories": {str(new): categories[str(old)] for old, new in node_ids.items() if str(old) in categories},
        "sources": {str(new): sources[str(old)] for old, new in frame_ids.items() if str(old) in sources},
        "tests": new_tests,
    }
