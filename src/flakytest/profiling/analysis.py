"""Turn the plugin's per-process records and the profiler's samples into a profile document.

See docs/profile-format.md for the document this builds.
"""

import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from flakytest.profiling import insights
from flakytest.profiling.frames import (
    CATEGORY_ORDER,
    Frame,
    FrameClassifier,
    frame_category,
    is_transparent,
    short_name,
    source_line,
    stack_category,
)
from flakytest.profiling.markers import MARKER_FILENAME, PHASES, parse_marker
from flakytest.profiling.stacks import RawFrame, read_samples

FORMAT = "flakytest-profile"
VERSION = 1
MAX_HOT_LINES = 15
MAX_SEGMENTS = 12
MAX_LONGREPR = 4000
LOW_SAMPLES = 5
TARGET_SAMPLES = 10
NICE_RATES = (1000, 2000, 5000, 10000, 20000)

# Hooks that tell where an unmarked sample was taken, checked from the leaf up
TIMELINE_HOOKS = {
    "pytest_collection": "collection",
    "perform_collect": "collection",
    "pytest_sessionfinish": "session_teardown",
    "pytest_unconfigure": "session_teardown",
    "pytest_runtestloop": "between_tests",
    "pytest_sessionstart": "startup",
    "pytest_configure": "startup",
    "pytest_cmdline_main": "startup",
    "_preparse": "startup",
    "pytest_load_initial_conftests": "startup",
    "pytest_plugin_registered": "startup",
    "main": "startup",
}


def r(value: float) -> float:
    return round(value, 6)


class ProfileBuilder:
    def __init__(self, rootdir: Path) -> None:
        self.classifier = FrameClassifier(rootdir)
        self.frames: list[list[Any]] = []
        self.frame_index: dict[Frame, int] = {}
        self.frame_files: dict[int, str] = {}
        self.stack_nodes: list[list[int]] = []
        self.stack_index: dict[tuple[int, int], int] = {}
        self.sources: dict[int, str] = {}
        self.stack_categories: dict[int, str] = {}
        self._leaf_sources: dict[tuple[str, int], str] = {}

    def intern_frame(self, raw: RawFrame) -> tuple[int, Frame]:
        frame = self.classifier.classify(*raw)
        index = self.frame_index.get(frame)
        if index is None:
            index = self.frame_index[frame] = len(self.frames)
            self.frames.append([frame.path, frame.func, frame.line, frame.kind])
            self.frame_files[index] = raw[0]
        return index, frame

    def intern_stack(self, frame_ids: list[int]) -> int:
        node = -1
        for frame_id in frame_ids:
            key = (node, frame_id)
            index = self.stack_index.get(key)
            if index is None:
                index = self.stack_index[key] = len(self.stack_nodes)
                self.stack_nodes.append([node, frame_id])
            node = index
        return node

    def leaf_source(self, raw: RawFrame) -> str:
        key = (raw[0], raw[2])
        text = self._leaf_sources.get(key)
        if text is None:
            text = self._leaf_sources[key] = source_line(raw[0], raw[2])
        return text

    def add_source(self, frame_id: int) -> None:
        if frame_id not in self.sources:
            self.sources[frame_id] = source_line(self.frame_files[frame_id], self.frames[frame_id][2])


def _main_thread(stacks: list[tuple[str, tuple[RawFrame, ...], int]]) -> str | None:
    marked: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for thread, frames, count in stacks:
        total[thread] += count
        if any(f[0] == MARKER_FILENAME for f in frames):
            marked[thread] += count
    if marked:
        return marked.most_common(1)[0][0]
    if total:
        return total.most_common(1)[0][0]
    return None


def _timeline_bucket(frames: tuple[RawFrame, ...]) -> str:
    for _, func, _ in reversed(frames):
        bucket = TIMELINE_HOOKS.get(func)
        if bucket:
            return bucket
    return "other"


def _importing_module(builder: ProfileBuilder, frames: tuple[RawFrame, ...]) -> str | None:
    """The outermost project module, else the outermost package module, being imported in a sample."""
    package = None
    for raw in frames:
        if raw[1] != "<module>" or raw[0].startswith("<"):
            continue
        frame = builder.classifier.classify(*raw)
        if frame.kind in ("test", "app"):
            return frame.path
        if package is None and frame.kind in ("lib", "stdlib") and not frame.path.startswith(("/", "~")):
            package = frame.path
    return package


def _segment_label(frames: list[Frame], position: int, test_func: str, phase: str) -> str:
    frame = frames[position]
    parent = frames[position - 1] if position else None
    if short_name(frame.func) == test_func:
        return "test body"
    if parent is not None and short_name(parent.func) in ("call_fixture_func", "_teardown_yield_fixture"):
        return f"fixture {short_name(frame.func)}" + (" (teardown)" if phase == "teardown" else "")
    return short_name(frame.func)


def _seconds(value: float) -> str:
    return f"{value:.1f}s" if value >= 1 else f"{value * 1000:.0f}ms"


def _nice_rate(rate: float) -> int:
    for nice in NICE_RATES:
        if nice >= rate:
            return nice
    return NICE_RATES[-1]


def build_profile(run: dict[str, Any], processes: list[dict[str, Any]], rootdir: Path, context: dict[str, Any]) -> dict:
    """run: the document's "run" section; processes: the plugin's records, one per pytest process."""
    builder = ProfileBuilder(rootdir)
    tests: list[dict[str, Any]] = []
    timeline: Counter[str] = Counter()
    imports: defaultdict[str, Counter[str]] = defaultdict(Counter)
    total_samples = 0
    sample_errors = 0
    rate = run["profiler"]["rate"]

    for process in processes:
        by_key: dict[str, dict[str, Any]] = {}
        for test in process["tests"]:
            test["_samples"] = {phase: Counter() for phase in PHASES}
            by_key[test["key"]] = test
            tests.append(test)
        samples_file = process.get("samples_file")
        if not samples_file or not Path(samples_file).exists():
            continue
        raw_stacks = read_samples(Path(samples_file))
        main = _main_thread(raw_stacks)
        main_samples = 0
        for thread, frames, count in raw_stacks:
            if thread != main:
                continue
            main_samples += count
            position = next((i for i in range(len(frames) - 1, -1, -1) if frames[i][0] == MARKER_FILENAME), None)
            marker = parse_marker(*frames[position][:2]) if position is not None else None
            if marker and marker[0] in PHASES and marker[1] in by_key:
                by_key[marker[1]]["_samples"][marker[0]][frames[position + 1 :]] += count  # type: ignore[operator]
                timeline["tests"] += count
            else:
                bucket = "collection" if marker and marker[0] == "collect" else _timeline_bucket(frames)
                timeline[bucket] += count
                if bucket in ("collection", "startup"):
                    module = _importing_module(builder, frames)
                    if module:
                        imports[bucket][module] += count
        total_samples += main_samples
        sample_errors += process.get("sample_errors", 0)
        process["_main_samples"] = main_samples

    seconds_per_sample = 1 / rate if rate else 0.0
    sampled_seconds = sum(p.get("sampled_seconds") or 0 for p in processes)
    if total_samples and sampled_seconds:
        seconds_per_sample = sampled_seconds / total_samples

    output_tests = [_build_test(builder, test) for test in tests]

    summary = _summary(output_tests, timeline, seconds_per_sample)
    summary["imports"] = {
        bucket: [
            {"module": module, "seconds": r(count * seconds_per_sample)} for module, count in counter.most_common(10)
        ]
        for bucket, counter in imports.items()
    }
    warnings = _warnings(output_tests, run, total_samples)
    run["profiler"]["samples"] = total_samples
    run["profiler"]["sample_errors"] = sample_errors
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "run": run,
        "summary": summary,
        "frames": builder.frames,
        "stacks": builder.stack_nodes,
        "stack_categories": {str(k): v for k, v in builder.stack_categories.items()},
        "sources": {str(k): v for k, v in builder.sources.items() if v},
        "tests": output_tests,
        "fixtures": _fixtures(output_tests, processes),
        "insights": [],
        "warnings": warnings,
    }
    doc["insights"] = insights.generate(doc, context)
    return doc


def _build_test(builder: ProfileBuilder, test: dict[str, Any]) -> dict[str, Any]:
    samples_by_phase: dict[str, Counter] = test.pop("_samples")
    phases = test["phases"]
    test_func = test["name"].split("[", 1)[0]
    stacks: dict[str, list[list[int]]] = {}
    sample_counts: dict[str, int] = {}
    categories: defaultdict[str, float] = defaultdict(float)
    segments: defaultdict[tuple[str, str, str], float] = defaultdict(float)
    segment_frames: dict[tuple[str, str, str], int | None] = {}
    hot_lines: dict[int, dict[str, Any]] = {}

    for phase in PHASES:
        counter = samples_by_phase[phase]
        phase_total = sum(counter.values())
        sample_counts[phase] = phase_total
        duration = phases.get(phase, 0.0)
        if not phase_total:
            if duration > 0:
                categories["unsampled"] += duration
            continue
        phase_stacks = []
        for raw_frames, count in counter.most_common():
            seconds = count / phase_total * duration
            ids = []
            frames = []
            hints = []
            for raw in raw_frames:
                frame_id, frame = builder.intern_frame(raw)
                ids.append(frame_id)
                frames.append(frame)
                hints.append(frame_category(frame.module, frame.func))
            leaf_source = builder.leaf_source(raw_frames[-1]) if raw_frames else ""
            category = stack_category(frames, hints, leaf_source)
            categories[category] += seconds
            node = builder.intern_stack(ids)
            builder.stack_categories[node] = category
            phase_stacks.append([node, count])

            position = next((i for i, f in enumerate(frames) if not is_transparent(f)), None)
            if position is None:
                segment = (phase, "pytest internals", "")
                segment_frames.setdefault(segment, None)
            else:
                frame = frames[position]
                segment = (phase, _segment_label(frames, position, test_func, phase), f"{frame.path}:{frame.func}")
                segment_frames.setdefault(segment, ids[position])
            segments[segment] += seconds

            for i in range(len(frames) - 1, -1, -1):
                if frames[i].kind in ("test", "app"):
                    line = hot_lines.setdefault(ids[i], {"frame": ids[i], "seconds": 0.0, "categories": Counter()})
                    line["seconds"] += seconds
                    line["categories"][category] += seconds
                    break
        stacks[phase] = phase_stacks

    top_segments = sorted(segments.items(), key=lambda item: -item[1])[:MAX_SEGMENTS]
    for segment, _ in top_segments:
        if segment_frames[segment] is not None:
            builder.add_source(segment_frames[segment])  # type: ignore[arg-type]
    top_lines = sorted(hot_lines.values(), key=lambda line: -line["seconds"])[:MAX_HOT_LINES]
    for line in top_lines:
        builder.add_source(line["frame"])
        line["seconds"] = r(line["seconds"])
        line["categories"] = {k: r(v) for k, v in line["categories"].most_common(4)}

    warnings = []
    total_samples = sum(sample_counts.values())
    if test["outcome"] != "skipped" and test["duration"] > 0:
        if total_samples == 0:
            warnings.append("undersampled")
        elif total_samples < LOW_SAMPLES:
            warnings.append("low_samples")

    longrepr = test.get("longrepr")
    return {
        "nodeid": test["nodeid"],
        "key": test["key"],
        "file": test["file"],
        "class": test.get("class"),
        "name": test["name"],
        "outcome": test["outcome"],
        "worker": test.get("worker"),
        "duration": r(test["duration"]),
        "phases": {phase: r(phases.get(phase, 0.0)) for phase in PHASES},
        "samples": sample_counts,
        "stacks": stacks,
        "fixtures": test.get("fixtures", []),
        # setup time of class/module/session fixtures this test happened to trigger first
        "shared_fixture_time": r(sum(f["duration"] for f in test.get("fixtures", []) if f["scope"] != "function")),
        "categories": {k: r(v) for k, v in sorted(categories.items(), key=_category_sort) if v > 0},
        "segments": [
            {"phase": segment[0], "label": segment[1], "frame": segment_frames[segment], "seconds": r(seconds)}
            for segment, seconds in top_segments
        ],
        "hot_lines": top_lines,
        "db": test.get("db"),
        "longrepr": longrepr[:MAX_LONGREPR] if longrepr else None,
        "warnings": warnings,
    }


def _category_sort(item: tuple[str, float]) -> tuple[int, float]:
    name, seconds = item
    return (CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER), -seconds)


def _summary(tests: list[dict[str, Any]], timeline: Counter[str], seconds_per_sample: float) -> dict[str, Any]:
    outcomes = Counter(test["outcome"] for test in tests)
    categories: defaultdict[str, float] = defaultdict(float)
    phases: defaultdict[str, float] = defaultdict(float)
    for test in tests:
        for category, seconds in test["categories"].items():
            categories[category] += seconds
        for phase, seconds in test["phases"].items():
            phases[phase] += seconds
    return {
        "tests": len(tests),
        "passed": outcomes["passed"] + outcomes["xpassed"],
        "failed": outcomes["failed"],
        "errors": outcomes["error"],
        "skipped": outcomes["skipped"] + outcomes["xfailed"],
        "test_time": r(sum(test["duration"] for test in tests)),
        "phases": {k: r(v) for k, v in phases.items()},
        "categories": {k: r(v) for k, v in sorted(categories.items(), key=lambda item: -item[1])},
        "timeline": {k: r(v * seconds_per_sample) for k, v in timeline.most_common()},
    }


def _fixtures(tests: list[dict[str, Any]], processes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    stats: dict[tuple[str, str], dict[str, Any]] = {}
    for test in tests:
        for fixture in test["fixtures"]:
            entry = stats.setdefault(
                (fixture["name"], fixture["scope"]),
                {"name": fixture["name"], "scope": fixture["scope"], "calls": 0, "total": 0.0, "max": 0.0},
            )
            entry["calls"] += 1
            entry["total"] += fixture["duration"]
            entry["max"] = max(entry["max"], fixture["duration"])
    used_by: Counter[str] = Counter()
    for process in processes:
        used_by.update(process.get("fixture_users", {}))
    result = []
    for entry in sorted(stats.values(), key=lambda e: -e["total"]):
        entry["total"] = r(entry["total"])
        entry["max"] = r(entry["max"])
        entry["mean"] = r(entry["total"] / entry["calls"])
        entry["tests"] = used_by.get(entry["name"], entry["calls"])
        result.append(entry)
    return result


def _warnings(tests: list[dict[str, Any]], run: dict[str, Any], total_samples: int) -> list[dict[str, Any]]:
    profiler = run["profiler"]
    if profiler["backend"] == "none":
        return [
            {
                "code": "profiler_unavailable",
                "message": f"No sampling profiler ran ({profiler.get('error')}). Durations, fixture times and "
                "query counts are exact, but there is no breakdown of where time goes inside tests.",
            }
        ]
    warnings: list[dict[str, Any]] = []
    if total_samples == 0:
        warnings.append(
            {
                "code": "no_samples",
                "message": f"{profiler['backend']} captured no samples ({profiler.get('error') or 'unknown error'}).",
            }
        )
        return warnings
    rate = profiler["rate"]
    interval = 1 / rate if rate else 0.001
    test_time = sum(t["duration"] for t in tests) or 1.0
    missed = [t for t in tests if "undersampled" in t["warnings"]]
    low = [t for t in tests if "low_samples" in t["warnings"]]
    # Missing a test much shorter than the sampling interval is expected; only warn when it hides real time
    unexpected = [t for t in missed if t["duration"] >= 3 * interval]
    hidden_time = sum(t["duration"] for t in missed + low)
    if unexpected or hidden_time >= 0.05 * test_time:
        durations = [t["duration"] for t in (unexpected or missed + low) if t["duration"] > 0]
        median = statistics.median(durations) if durations else interval
        recommended = _nice_rate(max(rate * 2, math.ceil(TARGET_SAMPLES / max(median, 1e-4))))
        parts = []
        if missed:
            parts.append(f"{len(missed)} test{'s' if len(missed) != 1 else ''} got no samples")
        if low:
            parts.append(f"{len(low)} got fewer than {LOW_SAMPLES}")
        warnings.append(
            {
                "code": "undersampled",
                "message": f"{' and '.join(parts)} at {rate} Hz ({_seconds(hidden_time)} of test time), so their "
                f"breakdowns are missing or rough. Rerun with --flakytest-rate {recommended} for more detail.",
                "tests": [t["nodeid"] for t in sorted(missed + low, key=lambda t: -t["duration"])][:50],
                "recommended_rate": recommended,
            }
        )
    elif missed:
        warnings.append(
            {
                "code": "short_tests",
                "message": f"{len(missed)} tests finished faster than one sample ({_seconds(interval)}), so they have "
                "timings but no breakdown. That's expected for tests this fast.",
                "tests": [t["nodeid"] for t in missed][:50],
            }
        )
    errors = profiler.get("sample_errors", 0)
    if errors and errors > total_samples * 0.1:
        warnings.append(
            {
                "code": "sample_errors",
                "message": f"{errors} samples could not be read ({errors / (errors + total_samples):.0%}).",
            }
        )
    return warnings
