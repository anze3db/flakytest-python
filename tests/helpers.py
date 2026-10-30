"""Build plugin records and sample files like the profiler would, without running one."""

import json
from pathlib import Path

from flakytest.profiling.fixtures import FixtureStats
from flakytest.profiling.markers import MARKER_FILENAME, marker_key, marker_name

# What `flakytest pytest` adds to pytest: the plugin
PROFILE = ("-p", "flakytest.hook")
FROM_CALL = ("/venv/lib/python3.15/site-packages/_pytest/runner.py", "CallInfo.from_call", 341)
ROOT_FRAMES = (
    ("/venv/bin/pytest", "<module>", 8),
    ("/venv/lib/python3.15/site-packages/_pytest/main.py", "pytest_runtestloop", 377),
)


def record(nodeid, *, setup=0.0, call=0.0, teardown=0.0, outcome="passed", fixtures=(), db=None):
    file, _, rest = nodeid.partition("::")
    parts = rest.split("::")
    return {
        "nodeid": nodeid,
        "key": marker_key(nodeid),
        "file": file,
        "class": parts[0] if len(parts) > 1 else None,
        "name": parts[-1],
        "outcome": outcome,
        "worker": None,
        "duration": setup + call + teardown,
        "phases": {"setup": setup, "call": call, "teardown": teardown},
        "fixtures": list(fixtures),
        "db": db,
        "longrepr": None,
    }


def phase_stack(nodeid, phase, *frames):
    """A sampled stack inside a test phase: pytest frames, the marker, then the given frames."""
    marker = (MARKER_FILENAME, marker_name(phase, marker_key(nodeid)), 1)
    return (*ROOT_FRAMES, marker, FROM_CALL, *frames)


def write_tachyon_samples(path: Path, stacks, threads=(), *, done=True, seconds=None):
    """A tachyon_sampler file. stacks: the main thread's [(frames from the root, count)]; threads: other threads'
    [(main thread's marker frame, thread id, frames from the root, count)]; seconds: how long the sampler sampled."""
    path = path.with_suffix(".jsonl")
    frame_ids: dict = {}
    node_ids: dict = {}
    nodes: list = []

    def leaf(frames):
        node = -1
        for frame in frames:
            key = (node, frame_ids.setdefault(tuple(frame), len(frame_ids)))
            if key not in node_ids:
                node_ids[key] = len(nodes)
                nodes.append(list(key))
            node = node_ids[key]
        return node

    main = [[leaf(frames), count] for frames, count in stacks]
    other = [
        [frame_ids.setdefault(tuple(m), len(frame_ids)), tid, leaf(frames), count] for m, tid, frames, count in threads
    ]
    line = {"frames": [list(f) for f in frame_ids], "nodes": nodes, "main": main, "threads": other, "failed": 0}
    if done:
        line["done"] = True
    if seconds is not None:
        line["seconds"] = seconds
    path.write_text(json.dumps(line) + "\n")
    return path


def fixture_stats(tests):
    """What the plugin adds up while tests run, from the fixtures in the records."""
    stats = FixtureStats()
    for test in tests:
        for fixture in test["fixtures"]:
            stats.add(fixture["name"], fixture["scope"], fixture["duration"], test["nodeid"])
    return stats.as_list()


def process(tests, samples_file=None, sampled_seconds=0.0, **extra):
    return {
        "pid": 1,
        "worker": None,
        "backend": "tachyon" if samples_file else "none",
        "rate": 1000,
        "backend_error": None,
        "samples_file": str(samples_file) if samples_file else None,
        "sampled_seconds": sampled_seconds,
        "tests": tests,
        "fixture_users": {},
        "fixture_stats": fixture_stats(tests),
        "context": {},
        **extra,
    }


def run_info(backend="tachyon", rate=1000, duration=10.0, workers=1, cpu_count=8):
    return {
        "id": "20261016-000000-abcdef",
        "started_at": "2026-10-16T00:00:00Z",
        "duration": duration,
        "exit_status": "OK",
        "argv": [],
        "flakytest_version": "test",
        "python": {"version": "3.15.0", "implementation": "cpython"},
        "platform": {"system": "Linux", "release": "6", "machine": "x86_64", "cpu_count": cpu_count},
        "environment": {"kind": "local", "name": "local:me@laptop", "provider": "local", "details": {}},
        "git": {},
        "profiler": {"backend": backend, "rate": rate, "samples": 0, "error": None},
        "workers": workers,
        "plugins": [],
    }
