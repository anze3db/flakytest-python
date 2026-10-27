"""Turn the plugin's per-process records and the profiler's samples into a profile document.

Every test process builds its part (build_part): under pytest-xdist the workers do that in parallel when their
session ends. The controller only merges the parts (merge) and adds the session-wide summary, warnings and insights.
See docs/profile-format.md for the document this builds.

Samples arrive as a prefix tree of stacks. What a sample needs (its category, which matchers it hits, its first
visible frame, the deepest project frame) is worked out once per tree node from its parent's, so no stack is ever
walked twice.
"""

import contextlib
import datetime as dt
import gc
import math
import statistics
from collections import Counter, defaultdict
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from flakytest.profiling import fixtures as fixture_stats
from flakytest.profiling import insights, memory
from flakytest.profiling.frames import (
    CATEGORY_ORDER,
    NO_STATE,
    Frame,
    FrameClassifier,
    State,
    descend,
    frame_category,
    is_transparent,
    resolve,
    short_name,
    source_line,
)
from flakytest.profiling.markers import MARKER_FILENAME, PHASES, parse_marker
from flakytest.profiling.matchers import MATCHERS, Matched, frame_masks, is_owner
from flakytest.profiling.stacks import Samples, read_samples

FORMAT = "flakytest-profile"
VERSION = 1
MAX_HOT_LINES = 15
FOLDED_HOT_LINES = 5
MAX_SEGMENTS = 12
MAX_LONGREPR = 4000
LOW_SAMPLES = 5
TARGET_SAMPLES = 10
NICE_RATES = (1000, 2000, 5000, 10000, 20000)
# Tests keep their own stacks (call trees) when they are this slow or among the slowest KEEP_STACKS of their
# process; the others are folded into one suite-wide tree, which keeps the profile small on large suites
KEEP_STACKS_SECONDS = 0.1
KEEP_STACKS = 2000
# Nodes of the suite-wide tree with less than this share of its time are merged into their parent
SUITE_MIN_SHARE = 0.0002
# A first fixture call this much slower than the fixture's median is warm-up: a one-off cost
WARMUP_MIN = 0.5
WARMUP_FACTOR = 10
WARMUP_IMPORTS_MIN = 0.05

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
    # unittest and Django's test runner
    "discover": "collection",
    "loadTestsFromName": "collection",
    "loadTestsFromNames": "collection",
    "build_suite": "collection",
    "setup_databases": "database_setup",
    "teardown_databases": "session_teardown",
    "runTests": "between_tests",
    "run_suite": "between_tests",
    # Django's --parallel workers: multiprocessing's pool loop waits for the next batch of tests in `worker`
    "_init_worker": "startup",
    "worker": "between_tests",
    "_run_subsuite": "between_tests",
}
IMPORT_BUCKETS = ("collection", "startup")
# What the propagation in Tree starts from at a root node
ROOT: tuple[Any, ...] = (-1, NO_STATE, -1, -1, -1, 0, None, (-1, -1))


def r(value: float) -> float:
    return round(value, 6)


@contextlib.contextmanager
def no_gc() -> Iterator[None]:
    """Building a profile allocates millions of small containers that live until the end: without this the cyclic
    garbage collector would repeatedly scan all of them, for nothing."""
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


class Output:
    """The frames, stack nodes, categories and source lines of a profile document, interned."""

    def __init__(self) -> None:
        self.frames: list[list[Any]] = []
        self.frame_index: dict[tuple[str, str, int, str], int] = {}
        self.frame_files: dict[int, str] = {}
        self.nodes: list[list[int]] = []
        self.node_index: dict[tuple[int, int], int] = {}
        self.categories: dict[int, str] = {}
        self.sources: dict[int, str] = {}

    def frame(self, frame: Frame, filename: str) -> int:
        key = (frame.path, frame.func, frame.line, frame.kind)
        index = self.frame_index.get(key)
        if index is None:
            index = self.frame_index[key] = len(self.frames)
            self.frames.append(list(key))
            self.frame_files[index] = filename
        return index

    def node(self, parent: int, frame: int) -> int:
        key = (parent, frame)
        index = self.node_index.get(key)
        if index is None:
            index = self.node_index[key] = len(self.nodes)
            self.nodes.append([parent, frame])
        return index

    def add_source(self, frame: int) -> None:
        if frame not in self.sources:
            self.sources[frame] = source_line(self.frame_files.get(frame, ""), self.frames[frame][2])


class Tree:
    """The sampled stacks of one process, with what every node needs worked out from its parent's."""

    def __init__(self, samples: Samples, classifier: FrameClassifier, out: Output) -> None:
        self.samples = samples
        self.out = out
        raw = samples.frames
        self.frames = [classifier.classify(*frame) for frame in raw]
        frames = self.frames
        self.hints = [frame_category(f.module, f.func) for f in frames]
        self.markers = {i: parse_marker(*frame[:2]) for i, frame in enumerate(raw) if frame[0] == MARKER_FILENAME}
        masks = [frame_masks(f) for f in frames]
        hooks = [TIMELINE_HOOKS.get(short_name(f.func)) for f in frames]
        visible = [not is_transparent(f) for f in frames]
        project = [f.kind in ("test", "app") for f in frames]
        owner = [is_owner(f) for f in frames]
        module = [f.func == "<module>" and not raw[i][0].startswith("<") for i, f in enumerate(frames)]

        n = len(samples.nodes)
        # The marker frame a node is at or below (-1: none), and whether it's the marker's own node
        self.marker = marker_of = [-1] * n
        self.is_marker = is_marker = bytearray(n)
        self.state: list[State] = [NO_STATE] * n
        self.first_visible = first_visible = [-1] * n  # node of the outermost visible frame below the marker
        self.project = project_frame = [-1] * n  # the deepest project frame (raw frame id)
        self.owner = owner_frame = [-1] * n  # the deepest frame that isn't stdlib, native or pytest
        self.mask = mask = [0] * n  # through matchers on the stack
        self.hook: list[str | None] = [None] * n
        hook = self.hook
        # what a startup/collection sample is importing: the outermost project module, else package module
        importing: list[tuple[int, int]] = [(-1, -1)] * n
        self.importing = importing
        state = self.state
        markers = self.markers
        hints = self.hints
        for node, (parent, frame) in enumerate(samples.nodes):
            if parent < 0:
                p_marker, p_state, p_visible, p_project, p_owner, p_mask, p_hook, p_import = ROOT
            else:
                p_marker, p_state, p_visible = marker_of[parent], state[parent], first_visible[parent]
                p_project, p_owner, p_mask = project_frame[parent], owner_frame[parent], mask[parent]
                p_hook, p_import = hook[parent], importing[parent]
            if p_marker < 0 and frame in markers:
                marker_of[node] = frame
                is_marker[node] = 1
                hook[node] = p_hook
                continue
            marker_of[node] = p_marker
            hint = hints[frame]
            state[node] = descend(p_state, hint) if hint is not None else p_state
            first_visible[node] = p_visible if p_visible >= 0 or not visible[frame] else node
            project_frame[node] = frame if project[frame] else p_project
            owner_frame[node] = frame if owner[frame] else p_owner
            mask[node] = p_mask | masks[frame][0]
            hook[node] = hooks[frame] or p_hook
            if p_marker < 0 and module[frame]:
                if project[frame] and p_import[0] < 0:
                    p_import = (frame, p_import[1])
                elif (
                    frames[frame].kind in ("lib", "stdlib")
                    and p_import[1] < 0
                    and not frames[frame].path.startswith(("/", "~"))
                ):
                    p_import = (p_import[0], frame)
            importing[node] = p_import
        self.owned_masks = [m[1] for m in masks]
        self._categories: dict[int, str] = {}
        self._sources: dict[int, str] = {}
        self._out_frames: dict[int, int] = {}
        self._out_nodes: dict[int, int] = {}

    def category(self, node: int) -> str:
        category = self._categories.get(node)
        if category is None:
            frame = self.samples.nodes[node][1]
            leaf = self.frames[frame]
            source = self._sources.get(frame)
            if source is None:
                raw = self.samples.frames[frame]
                source = self._sources[frame] = source_line(raw[0], raw[2]) if leaf.kind != "native" else ""
            category = self._categories[node] = resolve(self.state[node], leaf, self.hints[frame], source)
        return category

    def matcher_bits(self, node: int) -> int:
        bits = self.mask[node]
        owner = self.owner[node]
        if owner >= 0:
            bits |= self.owned_masks[owner]
        return bits

    def out_frame(self, frame: int) -> int:
        index = self._out_frames.get(frame)
        if index is None:
            index = self._out_frames[frame] = self.out.frame(self.frames[frame], self.samples.frames[frame][0])
        return index

    def out_node(self, node: int) -> int:
        """The output stack node for a node: below the marker for a test's own samples, the whole stack for other
        threads'."""
        index = self._out_nodes.get(node)
        if index is not None:
            return index
        chain = []
        nodes = self.samples.nodes
        while node >= 0 and not self.is_marker[node] and node not in self._out_nodes:
            chain.append(node)
            node = nodes[node][0]
        parent = self._out_nodes.get(node, -1) if node >= 0 else -1
        for item in reversed(chain):
            parent = self._out_nodes[item] = self.out.node(parent, self.out_frame(nodes[item][1]))
        return parent


def _seconds(value: float) -> str:
    return f"{value:.1f}s" if value >= 1 else f"{value * 1000:.0f}ms"


def _nice_rate(rate: float) -> int:
    for nice in NICE_RATES:
        if nice >= rate:
            return nice
    return NICE_RATES[-1]


def _segment_label(frame: Frame, parent: Frame | None, test_func: str, scopes: dict[str, str]) -> tuple[str, bool]:
    """A segment's label, and whether it's a class/module/session fixture that other tests share."""
    name = short_name(frame.func)
    if name == test_func:
        return "test body", False
    if parent is not None and short_name(parent.func) in ("call_fixture_func", "_teardown_yield_fixture"):
        scope = scopes.get(name, "function")
        if scope != "function":
            return f"fixture {name} ({scope} scope)", True
        return f"fixture {name}", False
    return name, False


class PartBuilder:
    def __init__(self, process: dict[str, Any], rootdir: Path, started: float) -> None:
        self.process = process
        self.started = started
        self.out = Output()
        self.classifier = FrameClassifier(rootdir)
        self.matched: dict[str, Matched] = defaultdict(Matched)
        self.suite: defaultdict[tuple[int, str], float] = defaultdict(float)
        self.timeline: Counter[str] = Counter()
        self.imports: defaultdict[str, Counter[int]] = defaultdict(Counter)
        self.tree = Tree(Samples(), self.classifier, self.out)
        self.sampled = False
        self.scopes = {f["name"]: f["scope"] for f in process.get("fixture_stats", [])}
        self.scopes.update({f["name"]: f["scope"] for t in process["tests"] for f in t.get("fixtures", [])})

    def build(self) -> dict[str, Any]:
        process = self.process
        tests = process["tests"]
        by_key = {test["key"]: test for test in tests}
        main: dict[str, dict[str, list[tuple[int, int]]]] = {}
        threads: dict[str, dict[str, list[tuple[int, int, int]]]] = {}
        total = 0
        samples = self._samples()
        if samples is not None:
            self.sampled = True
            tree = self.tree = Tree(samples, self.classifier, self.out)
            for node, count in samples.main.items():
                total += count
                marker = tree.marker[node]
                parsed = tree.markers.get(marker) if marker >= 0 else None
                key = (parsed[1] or "") if parsed else ""
                if parsed and parsed[0] in PHASES and key in by_key:
                    main.setdefault(key, {}).setdefault(parsed[0], []).append((node, count))
                    self.timeline["tests"] += count
                    continue
                bucket = "collection" if parsed and parsed[0] == "collect" else tree.hook[node] or "other"
                self.timeline[bucket] += count
                if bucket in IMPORT_BUCKETS:
                    project, package = tree.importing[node]
                    module = project if project >= 0 else package
                    if module >= 0:
                        self.imports[bucket][module] += count
            for (marker, thread, node), count in samples.threads.items():
                parsed = tree.markers.get(marker)
                key = (parsed[1] or "") if parsed else ""
                if parsed and parsed[0] in PHASES and key in by_key:
                    threads.setdefault(key, {}).setdefault(parsed[0], []).append((node, count, thread))
        seconds_per_sample = 1 / process["rate"] if process.get("rate") else 0.0
        if total and process.get("sampled_seconds"):
            seconds_per_sample = process["sampled_seconds"] / total

        kept = self._kept(tests)
        output_tests = [
            self._test(test, main.get(test["key"], {}), threads.get(test["key"], {}), keep=test["nodeid"] in kept)
            for test in tests
        ]
        self._warmup(output_tests)
        out = self.out
        return {
            "pid": process.get("pid"),
            "worker": process.get("worker"),
            "backend": process["backend"],
            "rate": process["rate"],
            "backend_error": process.get("backend_error"),
            "memory_source": process.get("memory_source"),
            "context": process.get("context", {}),
            "fixture_users": process.get("fixture_users", {}),
            "fixture_stats": process.get("fixture_stats", []),
            "samples": total,
            "sample_errors": samples.failed if samples is not None else 0,
            "samples_complete": samples.complete if samples is not None else True,
            "frames": out.frames,
            "stacks": out.nodes,
            "stack_categories": {str(k): v for k, v in out.categories.items()},
            "sources": {str(k): v for k, v in out.sources.items() if v},
            "tests": output_tests,
            "suite_stacks": self._suite_stacks(),
            "timeline": {k: r(v * seconds_per_sample) for k, v in self.timeline.items()},
            "imports": {
                bucket: [
                    [self.tree.out_frame(module), r(count * seconds_per_sample)]
                    for module, count in counter.most_common(10)
                ]
                for bucket, counter in self.imports.items()
            },
            "matched": {name: matched.as_dict() for name, matched in self.matched.items()},
        }

    def _samples(self) -> Samples | None:
        samples_file = self.process.get("samples_file")
        if not samples_file or not Path(samples_file).exists():
            return None
        try:
            return read_samples(Path(samples_file))
        except (OSError, ValueError):
            return None

    def _kept(self, tests: list[dict[str, Any]]) -> set[str]:
        slowest = sorted(tests, key=lambda t: -t["duration"])
        return {t["nodeid"] for i, t in enumerate(slowest) if i < KEEP_STACKS or t["duration"] >= KEEP_STACKS_SECONDS}

    def _test(
        self,
        test: dict[str, Any],
        main: dict[str, list[tuple[int, int]]],
        threads: dict[str, list[tuple[int, int, int]]],
        *,
        keep: bool,
    ) -> dict[str, Any]:
        tree, out = self.tree, self.out
        phases = test["phases"]
        nodeid = test["nodeid"]
        test_func = test["name"].split("[", 1)[0]
        stacks: dict[str, list[list[Any]]] = {}
        thread_stacks: dict[str, list[list[Any]]] = {}
        sample_counts: dict[str, int] = {}
        categories: defaultdict[str, float] = defaultdict(float)
        segments: defaultdict[tuple[str, str, str], float] = defaultdict(float)
        segment_frames: dict[tuple[str, str, str], int | None] = {}
        hot_lines: dict[int, dict[str, Any]] = {}
        shared_teardown = 0.0
        thread_ids: set[int] = set()
        thread_seconds = 0.0
        thread_categories: defaultdict[str, float] = defaultdict(float)
        matched = self.matched

        def account(node: int, seconds: float, category: str) -> None:
            """Time that counts: categories, hot lines, matchers and the suite tree."""
            categories[category] += seconds
            project = tree.project[node]
            location = tree.out_frame(project) if project >= 0 else -1
            if location >= 0:
                line = hot_lines.get(location)
                if line is None:
                    line = hot_lines[location] = {"frame": location, "seconds": 0.0, "categories": Counter()}
                line["seconds"] += seconds
                line["categories"][category] += seconds
            matched["category:" + category].add(nodeid, location, category, seconds)
            bits = tree.matcher_bits(node)
            while bits:
                low = bits & -bits
                matcher = MATCHERS[low.bit_length() - 1]
                if matcher.categories is None or category in matcher.categories:
                    matched[matcher.name].add(nodeid, location, category, seconds)
                bits ^= low
            if not keep:
                self.suite[(node, category)] += seconds

        for phase in PHASES:
            counter = main.get(phase, [])
            phase_total = sum(count for _, count in counter)
            sample_counts[phase] = phase_total
            duration = phases.get(phase, 0.0)
            if not phase_total:
                if duration > 0:
                    categories["unsampled"] += duration
                continue
            entries = []
            waiting = 0.0
            for node, count in counter:
                seconds = count / phase_total * duration
                if tree.is_marker[node]:  # sampled in the marker frame itself
                    categories["pytest"] += seconds
                    continue
                category = tree.category(node)
                entries.append((node, count, seconds, category))
                if category == "waiting":
                    waiting += seconds
            # While the main thread waited, other threads did the work: split the waiting time between them
            background = [
                (node, count, thread)
                for node, count, thread in threads.get(phase, [])
                if tree.category(node) != "waiting"
            ]
            background_total = sum(count for _, count, _ in background)
            replace_waiting = bool(background_total and waiting)
            phase_stacks = []
            for node, count, seconds, category in entries:
                if keep:
                    out_node = tree.out_node(node)
                    out.categories[out_node] = category
                    phase_stacks.append([out_node, count])
                if not (replace_waiting and category == "waiting"):
                    account(node, seconds, category)
                position = tree.first_visible[node]
                if position < 0:
                    segment = (phase, "pytest internals", "")
                    segment_frames.setdefault(segment, None)
                else:
                    nodes = tree.samples.nodes
                    frame = tree.frames[nodes[position][1]]
                    parent_node = nodes[position][0]
                    parent = (
                        tree.frames[nodes[parent_node][1]]
                        if parent_node >= 0 and not tree.is_marker[parent_node]
                        else None
                    )
                    label, shared = _segment_label(frame, parent, test_func, self.scopes)
                    if shared and phase == "teardown":
                        shared_teardown += seconds
                    segment = (phase, label, f"{frame.path}:{frame.func}")
                    segment_frames.setdefault(segment, tree.out_frame(nodes[position][1]))
                segments[segment] += seconds
            if replace_waiting:
                thread_phase = []
                for node, count, thread in background:
                    seconds = count / background_total * waiting
                    category = tree.category(node)
                    account(node, seconds, category)
                    thread_ids.add(thread)
                    thread_seconds += seconds
                    thread_categories[category] += seconds
                    if keep:
                        out_node = tree.out_node(node)
                        out.categories[out_node] = category
                        thread_phase.append([out_node, r(seconds)])
                if thread_phase:
                    thread_stacks[phase] = thread_phase
            if keep:
                stacks[phase] = phase_stacks

        top_segments = sorted(segments.items(), key=lambda item: -item[1])[:MAX_SEGMENTS]
        for segment, _ in top_segments:
            if segment_frames[segment] is not None:
                out.add_source(segment_frames[segment])  # type: ignore[arg-type]
        top_lines = sorted(hot_lines.values(), key=lambda line: -line["seconds"])[
            : MAX_HOT_LINES if keep else FOLDED_HOT_LINES
        ]
        for line in top_lines:
            out.add_source(line["frame"])
            line["seconds"] = r(line["seconds"])
            line["categories"] = {k: r(v) for k, v in line["categories"].most_common(4)}

        warnings = []
        total_samples = sum(sample_counts.values())
        if test["outcome"] != "skipped" and test["duration"] > 0 and self.sampled:
            if total_samples == 0:
                warnings.append("undersampled")
            elif total_samples < LOW_SAMPLES:
                warnings.append("low_samples")

        fixtures = test.get("fixtures", [])
        longrepr = test.get("longrepr")
        result = {
            "nodeid": nodeid,
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
            "fixtures": fixtures,
            # class/module/session fixture setup (and teardown) this test happened to trigger
            "shared_fixture_time": r(
                sum(f["duration"] for f in fixtures if f["scope"] != "function") + shared_teardown
            ),
            "categories": {k: r(v) for k, v in sorted(categories.items(), key=_category_sort) if v > 0},
            "segments": [
                {"phase": segment[0], "label": segment[1], "frame": segment_frames[segment], "seconds": r(seconds)}
                for segment, seconds in top_segments
            ],
            "hot_lines": top_lines,
            "db": test.get("db"),
            "memory": memory.test_memory(test.get("memory"), self.started),
            "longrepr": longrepr[:MAX_LONGREPR] if longrepr else None,
            "warnings": warnings,
        }
        if thread_ids:
            result["threads"] = {
                "count": len(thread_ids),
                "seconds": r(thread_seconds),
                "categories": {k: r(v) for k, v in sorted(thread_categories.items(), key=lambda kv: -kv[1])},
            }
            if thread_stacks:
                result["thread_stacks"] = thread_stacks
        if test.get("unsampled_reason"):
            result["warnings"].append(test["unsampled_reason"])
        return result

    def _warmup(self, tests: list[dict[str, Any]]) -> None:
        """One-off costs a test happened to pay: the first, much slower call of a fixture in its process (starting
        something every later call reuses), and imports."""
        causes: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
        worker = self.process.get("worker")
        where = f" in {worker}" if worker else ""
        for entry in self.process.get("fixture_stats", []):
            first = entry.get("first") or {}
            if entry["scope"] != "function" or entry["calls"] < 2 or not first.get("nodeid"):
                continue
            usual = fixture_stats.median(entry)
            if first["duration"] >= WARMUP_MIN and first["duration"] >= WARMUP_FACTOR * usual:
                causes[first["nodeid"]].append(
                    {"what": f"first {entry['name']} call{where}", "seconds": r(first["duration"] - usual)}
                )
        for test in tests:
            imports = test["categories"].get("imports", 0.0)
            if imports >= WARMUP_IMPORTS_MIN:
                causes[test["nodeid"]].append({"what": "imports", "seconds": r(imports)})
        for test in tests:
            found = causes.get(test["nodeid"])
            if found:
                seconds = min(test["duration"], sum(c["seconds"] for c in found))
                test["warmup"] = {"seconds": r(seconds), "causes": found}

    def _suite_stacks(self) -> list[list[Any]]:
        """The folded tests' samples as one tree, with small nodes merged into their parents."""
        if not self.suite:
            return []
        tree = self.tree
        nodes = tree.samples.nodes
        totals = [0.0] * len(nodes)
        for (node, _), seconds in self.suite.items():
            totals[node] += seconds
        grand = sum(totals)
        for node in range(len(nodes) - 1, -1, -1):  # children have higher ids than their parents
            parent = nodes[node][0]
            if totals[node] and parent >= 0 and not tree.is_marker[parent]:
                totals[parent] += totals[node]
        threshold = grand * SUITE_MIN_SHARE
        kept: defaultdict[tuple[int, str], float] = defaultdict(float)
        for (leaf, category), seconds in self.suite.items():
            node = leaf
            while totals[node] < threshold:
                parent = nodes[node][0]
                if parent < 0 or tree.is_marker[parent]:
                    break
                node = parent
            out_node = tree.out_node(node)
            self.out.categories.setdefault(out_node, category)
            kept[(out_node, category)] += seconds
        return [[node, category, r(seconds)] for (node, category), seconds in kept.items()]


def build_part(process: dict[str, Any], rootdir: Path, started_at: float) -> dict[str, Any]:
    """One process's part of the profile. process: the plugin's record of it."""
    with no_gc():
        return PartBuilder(process, rootdir, started_at).build()


def _category_sort(item: tuple[str, float]) -> tuple[int, float]:
    name, seconds = item
    return (CATEGORY_ORDER.index(name) if name in CATEGORY_ORDER else len(CATEGORY_ORDER), -seconds)


def merge(run: dict[str, Any], parts: list[dict[str, Any]], rootdir: Path, context: dict[str, Any]) -> dict:
    """The profile document from every process's part. run: the document's "run" section."""
    with no_gc():
        return _merge(run, parts, rootdir, context)


def _merge(run: dict[str, Any], parts: list[dict[str, Any]], rootdir: Path, context: dict[str, Any]) -> dict:
    frames: list[list[Any]] = []
    frame_index: dict[tuple[Any, ...], int] = {}
    nodes: list[list[int]] = []
    node_index: dict[tuple[int, int], int] = {}
    stack_categories: dict[str, str] = {}
    sources: dict[str, str] = {}
    tests: list[dict[str, Any]] = []
    suite_stacks: defaultdict[tuple[int, str], float] = defaultdict(float)
    timeline: Counter[str] = Counter()
    imports: defaultdict[str, Counter[int]] = defaultdict(Counter)
    matched: dict[str, Matched] = defaultdict(Matched)
    total_samples = sample_errors = 0
    incomplete = []
    for part in parts:
        frame_ids = []
        for frame in part["frames"]:
            key = tuple(frame)
            index = frame_index.get(key)
            if index is None:
                index = frame_index[key] = len(frames)
                frames.append(frame)
            frame_ids.append(index)
        node_ids: list[int] = []
        for parent, frame in part["stacks"]:
            key = (node_ids[parent] if parent >= 0 else -1, frame_ids[frame])
            index = node_index.get(key)
            if index is None:
                index = node_index[key] = len(nodes)
                nodes.append(list(key))
            node_ids.append(index)
        for node, category in part["stack_categories"].items():
            stack_categories[str(node_ids[int(node)])] = category
        for frame, text in part["sources"].items():
            sources[str(frame_ids[int(frame)])] = text
        for test in part["tests"]:
            test["stacks"] = {
                phase: [[node_ids[node], count] for node, count in pairs] for phase, pairs in test["stacks"].items()
            }
            if "thread_stacks" in test:
                test["thread_stacks"] = {
                    phase: [[node_ids[node], seconds] for node, seconds in pairs]
                    for phase, pairs in test["thread_stacks"].items()
                }
            for segment in test["segments"]:
                if segment["frame"] is not None:
                    segment["frame"] = frame_ids[segment["frame"]]
            for line in test["hot_lines"]:
                line["frame"] = frame_ids[line["frame"]]
            tests.append(test)
        for node, category, seconds in part["suite_stacks"]:
            suite_stacks[(node_ids[node], category)] += seconds
        timeline.update(part["timeline"])
        for bucket, modules in part["imports"].items():
            for frame, seconds in modules:
                imports[bucket][frame_ids[frame]] += seconds
        for name, data in part["matched"].items():
            matched[name].merge(Matched.from_dict(data), frame_ids)
        total_samples += part["samples"]
        sample_errors += part["sample_errors"]
        if part["backend"] != "none" and not part.get("samples_complete", True):
            incomplete.append(part.get("worker") or "main")

    summary = _summary(tests, timeline)
    source = next((p["memory_source"] for p in parts if p.get("tests") and p.get("memory_source")), None)
    summary["memory"] = memory.summarize(tests, source)
    summary["imports"] = {
        bucket: [
            {"module": frames[frame][0], "seconds": r(seconds)}
            for frame, seconds in sorted(counter.items(), key=lambda kv: -kv[1])[:10]
        ]
        for bucket, counter in imports.items()
    }
    run["profiler"]["samples"] = total_samples
    run["profiler"]["sample_errors"] = sample_errors
    warnings = _warnings(tests, run, total_samples)
    if growth := memory.growth_warning(summary["memory"], tests):
        warnings.append(growth)
    if incomplete:
        warnings.append(
            {
                "code": "samples_incomplete",
                "message": f"The profiler of {', '.join(incomplete)} didn't finish writing its samples, so the "
                "breakdowns of its last tests are missing; their timings are exact.",
            }
        )
    doc = {
        "format": FORMAT,
        "version": VERSION,
        "run": run,
        "summary": summary,
        "frames": frames,
        "stacks": nodes,
        "stack_categories": stack_categories,
        "sources": sources,
        "tests": tests,
        "suite_stacks": [[node, category, r(seconds)] for (node, category), seconds in suite_stacks.items()],
        "fixtures": _fixtures(parts),
        "insights": [],
        "warnings": warnings,
    }
    doc["insights"] = insights.generate(doc, context, matched, rootdir)
    _add_sources(doc, rootdir)
    return doc


def _add_sources(doc: dict[str, Any], rootdir: Path) -> None:
    """Source lines for the project lines the insights point at."""
    for item in doc["insights"]:
        for location in item.get("locations", []):
            key = str(location["frame"])
            if key not in doc["sources"]:
                path, _, line, kind = doc["frames"][location["frame"]]
                if kind in ("test", "app") and (text := source_line(str(rootdir / path), line)):
                    doc["sources"][key] = text


def _summary(tests: list[dict[str, Any]], timeline: Counter[str]) -> dict[str, Any]:
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
        "timeline": {k: r(v) for k, v in timeline.most_common()},
    }


def _fixtures(parts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    used_by: Counter[str] = Counter()
    for part in parts:
        used_by.update(part.get("fixture_users", {}))
    result = fixture_stats.merge([part.get("fixture_stats", []) for part in parts])
    for entry in result:
        entry["tests"] = used_by.get(entry["name"], entry["calls"])
    return result


def rerun_command(runner: str, rate: int, nodeids: list[str]) -> str:
    """The command that reruns some tests at a sampling rate, for the runner the run used."""
    if runner == "pytest":
        return f"flakytest pytest --flakytest-rate {rate} {' '.join(nodeids)}"
    # unittest names tests by module: shop/tests/test_orders.py::TestCheckout::test_list is
    # shop.tests.test_orders.TestCheckout.test_list
    names = " ".join(nodeid.replace(".py::", "::").replace("::", ".").replace("/", ".") for nodeid in nodeids)
    command = "manage.py test" if runner == "django" else "-m unittest"
    return f"flakytest python --flakytest-rate {rate} {command} {names}"


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
        affected = sorted(missed + low, key=lambda t: -t["duration"])
        durations = [t["duration"] for t in (unexpected or affected) if t["duration"] > 0]
        median = statistics.median(durations) if durations else interval
        recommended = _nice_rate(max(rate * 2, math.ceil(TARGET_SAMPLES / max(median, 1e-4))))
        parts = []
        if missed:
            parts.append(f"{len(missed)} test{'s' if len(missed) != 1 else ''} got no samples")
        if low:
            parts.append(f"{len(low)} got fewer than {LOW_SAMPLES}")
        rerun = rerun_command(run.get("runner") or "pytest", recommended, [t["nodeid"] for t in affected[:3]])
        warnings.append(
            {
                "code": "undersampled",
                "message": f"{' and '.join(parts)} at {rate} Hz ({_seconds(hidden_time)} of test time), so their "
                f"breakdowns are missing or rough. For detail on the ones you care about, rerun just them at a "
                f"higher rate, e.g. {rerun}",
                "tests": [t["nodeid"] for t in affected][:50],
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


def build_profile(run: dict[str, Any], processes: list[dict[str, Any]], rootdir: Path, context: dict[str, Any]) -> dict:
    """The whole profile in one go: every process's part, merged. run: the document's "run" section; processes: the
    plugin's records, one per pytest process."""
    started = dt.datetime.fromisoformat(run["started_at"].replace("Z", "+00:00")).timestamp()
    parts = [build_part(process, rootdir, started) for process in processes]
    return merge(run, parts, rootdir, context)
