"""A profiling session, whatever runs the tests: start the sampler, keep a record of every test (its phases, memory,
queries and shared fixtures), and build the profile and report at the end.

The pytest plugin (plugin.py) and the unittest / Django runner support (unittest_support.py) drive it.

Every test process records its tests as they finish (tests-<pid>.jsonl) and builds its part of the profile when it's
done (part-<pid>.json). Under pytest-xdist or Django's --parallel the workers do that at the same time; the process
that started them merges the parts, and fills in the tests of a worker that crashed from what it recorded, or else
from the reports it got.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from flakytest import cloud
from flakytest.__about__ import __version__
from flakytest.environment import detect_environment, get_git_data, platform_info, python_info
from flakytest.profiling import analysis
from flakytest.profiling.dbtrack import QueryStats, QueryTracker
from flakytest.profiling.fixtures import SLOW_FIXTURE, FixtureStats
from flakytest.profiling.markers import PhaseMarkers, make_marker, marker_key
from flakytest.profiling.memory import MemoryMeter, mb
from flakytest.profiling.samplers import DEFAULT_RATES, Sampler, choose_backend
from flakytest.report import html, runs, text

RUN_DIR_ENV = "FLAKYTEST_RUN_DIR"
WORKER_WAIT = 30.0
SAMPLER_WAIT = 30.0
# Big suites are sampled less often (unless the rate is set explicitly): suite-wide insights don't need more, and
# every sample slows the tests down a little
LARGE_SUITE = 10_000
LARGE_SUITE_RATE = 500
BUSY_PY_SPY_RATE = 100
SAVE_EVERY = 1.0  # seconds between writing the records of finished tests to disk
RUN_FILE = "run.json"  # what the run is, for a report of it if it never finishes
# Crashed workers that needed this share of the machine's memory together were probably killed for running out of
# it: the rest goes to the system, the database, the profilers and pytest-xdist's controller
OUT_OF_MEMORY_SHARE = 0.6


def new_record(nodeid: str, file: str, cls: str | None, name: str, worker: str | None) -> dict[str, Any]:
    return {
        "nodeid": nodeid,
        "key": marker_key(nodeid),
        "file": file,
        "class": cls,
        "name": name,
        "outcome": "passed",
        "worker": worker,
        "duration": 0.0,
        "phases": {},
        "fixtures": [],
        "longrepr": None,
    }


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str))
    tmp.replace(path)


def r(value: float) -> float:
    return round(value, 6)


class Session:
    """One per test process.

    controller: a pytest-xdist controller, which runs no tests and doesn't sample, only merges.
    worker: the worker's name (gw0, django-1, …) when this process is one of several that run tests.
    """

    active: Session | None = None

    def __init__(
        self,
        rootdir: Path,
        *,
        runner: str,
        argv: list[str],
        output: str | None = None,
        backend: str = "auto",
        rate: int | None = None,
        worker: str | None = None,
        controller: bool = False,
        workers: int = 0,
    ) -> None:
        self.rootdir = rootdir
        self.runner = runner
        self.argv = argv
        self.started_at = dt.datetime.now(dt.UTC)
        self.started = time.perf_counter()
        self.worker = worker
        self.controller = controller
        self.output = output
        self.run_dir = self._run_dir()
        self.backend, self.backend_error = choose_backend(backend)
        if controller:
            self.backend, self.backend_error = "none", None
        self.explicit_rate = rate
        self.rate = rate or DEFAULT_RATES.get(self.backend, 1000)
        if self.backend == "py-spy" and not rate and workers >= (os.cpu_count() or 1):
            # py-spy needs a lot of CPU per sample: with every CPU running a worker, profilers at the usual rate
            # would slow the tests down several times
            self.rate = BUSY_PY_SPY_RATE
        self.sampler: Sampler | None = None
        self.markers = PhaseMarkers()
        self.queries = QueryTracker()
        self.memory = MemoryMeter()
        self.tests: dict[str, dict[str, Any]] = {}
        self.current: dict[str, Any] | None = None
        self.current_memory: dict[str, Any] | None = None
        self.current_queries: QueryStats | None = None
        # The last test, until the next one starts: what it kept is measured then
        self.pending: tuple[dict[str, Any], dict[str, Any] | None] | None = None
        self.classes_seen: set[tuple[str, str | None]] = set()
        self.fixture_users: Counter[str] = Counter()
        self.fixture_stats = FixtureStats()
        self.django_debug: bool | None = None
        self.tests_file: Any = None
        self.saved_at = 0.0
        self.facts: Background | None = None
        # What the process that started the workers knows about every test (pytest-xdist reports), and the workers
        # that crashed
        self.reports: dict[str, dict[str, Any]] = {}
        self.crashed: dict[str, str] = {}
        self.last_report = self.started
        self.context: dict[str, Any] = {}
        self.profile: dict[str, Any] | None = None
        self.report_path: Path | None = None
        self.upload_url: str | None = None

    def _run_dir(self) -> Path:
        shared = os.environ.get(RUN_DIR_ENV)
        if shared and self.worker:
            return Path(shared)
        runs_dir = Path(self.output) if self.output else runs.runs_dir(self.rootdir)
        run_dir = runs_dir / f"{self.started_at:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        runs.ignore_in_git(runs_dir)
        # Workers (pytest-xdist's, Django's --parallel) inherit the environment, so they write into the same directory
        os.environ[RUN_DIR_ENV] = str(run_dir)
        return run_dir

    @property
    def pid(self) -> int:
        return os.getpid()

    def start(self, *, markers: bool = True) -> None:
        """markers: patch pytest so every test phase runs in a marker frame (pytest only)."""
        Session.active = self
        if self.backend != "none":
            self.sampler = Sampler(self.backend, self.rate, self.run_dir)
            if not self.sampler.start():
                self.backend_error = self.sampler.error
                self.backend = "none"
                self.sampler = None
        if self.sampler is not None and markers:  # only samples need to know which test they were taken in
            self.markers.install()
        if not self.controller:  # what a process that crashes recorded can still go in the report
            self.tests_file = (self.run_dir / f"tests-{self.pid}.jsonl").open("a", encoding="utf-8")
            self._write_meta("running")
        if not self.worker:  # git runs a few subprocesses: let them run while the tests do
            write_json(self.run_dir / RUN_FILE, self._run_base())
            self.facts = Background(self._facts)

    @property
    def sampling(self) -> bool:
        return self.sampler is not None

    def _process(self, status: str) -> dict[str, Any]:
        sampler = self.sampler
        return {
            "pid": self.pid,
            "status": status,
            "worker": self.worker,
            "backend": self.backend,
            "rate": self.rate,
            # what it sampled tests at: big suites are sampled less often than the sampler started with
            "sampling_rate": sampler.sampling_rate if sampler else self.rate,
            "backend_error": self.backend_error,
            "samples_file": str(sampler.out) if sampler else None,
            "sampler_pid": sampler.process.pid if sampler and sampler.process else None,
            "sampled_seconds": (sampler.stopped_at or time.perf_counter()) - sampler.started_at if sampler else 0.0,
            "memory_source": self.memory.source,
            "started_at": self.started_at.timestamp(),
        }

    def _write_meta(self, status: str, **extra: Any) -> None:
        write_json(self.run_dir / f"meta-{self.pid}.json", self._process(status) | extra)

    def collected(self, count: int) -> None:
        if self.sampler is not None and not self.explicit_rate and count > LARGE_SUITE:
            self.sampler.set_rate(LARGE_SUITE_RATE)

    def begin_test(self, nodeid: str, file: str, cls: str | None, name: str) -> dict[str, Any]:
        record = new_record(nodeid, file, cls, name, self.worker)
        self.tests[nodeid] = record
        if self.sampler is not None:
            self.sampler.next_test()
        self.queries.install()
        stats = QueryStats()
        reading = self.memory.start()
        self._finish_pending(reading)
        if reading is not None:
            group = (record["file"], record["class"])
            if not self.classes_seen:
                reading["warmup"] = "first test in its process"
            elif record["class"] and group not in self.classes_seen:
                reading["warmup"] = "first test in its class"
            self.classes_seen.add(group)
        self.current = record
        self.current_memory = reading
        self.current_queries = stats
        self.queries.current = stats
        self.markers.current_key = record["key"]
        return record

    def end_test(self, record: dict[str, Any]) -> None:
        self.markers.current_key = None
        self.queries.current = None
        self.current = None
        stats = self.current_queries
        if stats is not None and stats.queries:
            record["db"] = stats.as_dict()
        self.current_queries = None
        record["duration"] = sum(record["phases"].values())
        self.pending = (record, self.current_memory)
        self.current_memory = None
        self.last_report = time.perf_counter()
        if self.django_debug is None and "call" in record["phases"]:
            # pytest-django and Django's runner set DEBUG for the tests, and restore it at the end
            self.django_debug = _django_debug()

    @contextlib.contextmanager
    def phase(self, record: dict[str, Any], phase: str) -> Iterator[Callable[..., Any]]:
        """Time a test phase (or a piece of one) into the record. Yields how to call the code: inside a marker frame
        when sampling, so its samples are this test's."""
        started = time.perf_counter()
        try:
            yield make_marker(phase, record["key"]) if self.sampler is not None else _call
        finally:
            record["phases"][phase] = record["phases"].get(phase, 0.0) + time.perf_counter() - started

    def fixture_started(self, *, shared: bool) -> tuple[Any, ...]:
        """Call before a fixture's setup. A class, module or session fixture's queries and memory are its own, not the
        test's that happens to run it."""
        if self.current is None or not shared:
            return (time.perf_counter(), None, None, None)
        queries, previous = QueryStats(), self.queries.current
        self.queries.current = queries
        return (time.perf_counter(), queries, previous, self.memory.read())

    def fixture_finished(self, token: tuple[Any, ...], name: str, scope: str) -> None:
        started, queries, previous, before = token
        duration = time.perf_counter() - started
        record = self.current
        shared = scope != "function"
        self.fixture_stats.add(name, scope, duration, record["nodeid"] if record is not None else None)
        if record is None or not (shared or duration >= SLOW_FIXTURE):
            return
        entry: dict[str, Any] = {"name": name, "scope": scope, "duration": r(duration)}
        record["fixtures"].append(entry)
        if shared:
            self.queries.current = previous
            if queries is not None and queries.queries:
                entry["db"] = queries.as_dict()
            after = self.memory.read()
            if before and after and before.get("private") is not None and after.get("private") is not None:
                entry["memory"] = after["private"] - before["private"]
            self.current_memory = self.memory.restart(self.current_memory)

    def _finish_pending(self, reading: dict[str, Any] | None) -> None:
        """Measure what the last test kept, and record it for good."""
        if self.pending is None:
            return
        record, started = self.pending
        self.pending = None
        record["duration"] = sum(record["phases"].values())  # unittest adds a class teardown after the test
        if used := self.memory.stop(started, reading):
            if started and started.get("warmup"):
                used["warmup"] = started["warmup"]
            record["memory"] = used
        self._save(record)

    def _save(self, record: dict[str, Any]) -> None:
        """Append a finished test to this process's records, so they survive the process crashing: written at most
        a second later (the reports the parent gets fill in the tests a crash loses)."""
        if self.tests_file is not None:
            with contextlib.suppress(OSError, ValueError):
                self.tests_file.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                if (now := time.monotonic()) - self.saved_at >= SAVE_EVERY:
                    self.tests_file.flush()
                    self.saved_at = now

    def finish(self, exit_status: str, context: dict[str, Any] | None = None) -> None:
        """At the end of the session: this process's part of the profile, then (unless it's a worker) the profile."""
        self.markers.uninstall()
        if context is not None:
            self.context = context
        try:
            if not self.controller:
                self._finish_pending(self.memory.read())
                self._build_part()
            if not self.worker:
                self._build(exit_status)
        except Exception as e:  # never fail the test run because the report couldn't be built
            sys.stderr.write(f"\nflakytest: could not build the profile: {type(e).__name__}: {e}\n")
            if self.worker:  # don't keep the parent waiting for this worker's part
                with contextlib.suppress(OSError):
                    self._write_meta("failed")
        finally:
            if self.tests_file is not None:
                with contextlib.suppress(OSError):
                    self.tests_file.close()
            if not self.worker:
                Session.active = None
                os.environ.pop(RUN_DIR_ENV, None)

    def _build_part(self) -> None:
        """This process's part of the profile (with several workers: built in every worker at once)."""
        started = time.perf_counter()
        self._write_meta("stopping")  # before the sampler stops: if it takes the process down, the tests survive
        if self.tests_file is not None:
            with contextlib.suppress(OSError, ValueError):
                self.tests_file.flush()
        if self.sampler is not None:
            if self.sampler.stop() is None:
                self.backend_error = self.sampler.error
        process = self._process("done") | {
            "tests": list(self.tests.values()),
            "fixture_users": dict(self.fixture_users),
            "fixture_stats": self.fixture_stats.as_list(),
            "context": self.full_context(),
        }
        part = analysis.build_part(process, self.rootdir, self.started_at.timestamp())
        part["build_seconds"] = round(time.perf_counter() - started, 3)
        write_json(self.run_dir / f"part-{self.pid}.json", part)
        self._write_meta("done")

    def _parts(self) -> list[dict[str, Any]]:
        """Every process's part. A worker that crashed left no part: build it from its records and samples."""
        deadline = time.monotonic() + WORKER_WAIT
        while True:
            metas = [json.loads(path.read_text()) for path in self.run_dir.glob("meta-*.json")]
            unfinished = [
                m
                for m in metas
                if m["status"] != "failed"
                and m["worker"] not in self.crashed
                and _running(m["pid"])
                and not (self.run_dir / f"part-{m['pid']}.json").exists()
            ]
            if not unfinished or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        parts = []
        for path in sorted(self.run_dir.glob("meta-*.json")):
            meta = json.loads(path.read_text())
            part_path = self.run_dir / f"part-{meta['pid']}.json"
            if part_path.exists():
                parts.append(json.loads(part_path.read_text()))
            else:
                if meta["worker"] and meta["worker"] not in self.crashed:
                    self.crashed[meta["worker"]] = "exited without writing its profile"
                parts.append(self._recover(meta))
        return parts

    def _recover(self, meta: dict[str, Any]) -> dict[str, Any]:
        """The part of a worker that died before building it (its profiler notices the worker is gone and writes
        what it has: give it a moment)."""
        sampler_pid = meta.get("sampler_pid")
        deadline = time.monotonic() + SAMPLER_WAIT
        while sampler_pid and _running(sampler_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        return recover_part(self.run_dir, meta, self.rootdir, self.started_at.timestamp(), self.reports)

    def _backfill(self, parts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
        """A part for the tests that were reported but that no process recorded: exact timings and outcomes only."""
        recorded = {test["nodeid"] for part in parts for test in part["tests"]}
        missing = [record for nodeid, record in self.reports.items() if nodeid not in recorded]
        if not missing:
            return None, 0
        for record in missing:
            record["unsampled_reason"] = "worker_crashed" if record.get("worker") in self.crashed else "not_recorded"
        process: dict[str, Any] = {
            "pid": None,
            "worker": None,
            "backend": "none",
            "rate": self.rate,
            "tests": missing,
            "fixture_users": {},
            "fixture_stats": [],
            "context": {},
        }
        return analysis.build_part(process, self.rootdir, self.started_at.timestamp()), len(missing)

    def _build(self, exit_status: str) -> None:
        started = time.perf_counter()
        parts = self._parts()
        backfill, missing = self._backfill(parts)
        recovered = [p.get("worker") for p in parts if p.get("worker") in self.crashed]
        if backfill is not None:
            parts.append(backfill)
        facts = self.facts.result() if self.facts else {"environment": detect_environment(), "git": get_git_data()}
        base = self._run_base() | facts
        base |= {"duration": round(time.perf_counter() - self.started, 6), "exit_status": exit_status}
        run, context = describe_run(base, parts, self.rate, self.full_context())
        self.profile = analysis.merge(run, parts, self.rootdir, context)
        # How long building the profile took after the last test: the processes' parts (built at the same time with
        # several workers), then merging them. Writing the files comes on top.
        run["postprocessing"] = {
            "tests_finished_at": round(self.last_report - self.started, 3),  # seconds into the session
            "parts": max((p.get("build_seconds") or 0.0 for p in parts), default=0.0),
            "merge": round(time.perf_counter() - started, 3),
        }
        if self.crashed or missing:
            self.profile["warnings"].insert(0, crash_warning(self.profile, self.crashed, recovered, missing))
        self.report_path = write_outputs(self.profile, self.run_dir)
        runs.mark_latest(self.run_dir)
        leftovers = [
            self.run_dir / RUN_FILE,
            *self.run_dir.glob("meta-*.json"),
            *self.run_dir.glob("part-*.json"),
            *self.run_dir.glob("tests-*.jsonl"),
            *self.run_dir.glob("samples-*"),
            *self.run_dir.glob("control-*"),
            *self.run_dir.glob("*.ready"),
        ]
        if not run["profiler"]["error"] and not self.crashed:  # keep the profiler's log when something went wrong
            leftovers += self.run_dir.glob("sampler-*.log")
        for leftover in leftovers:
            with contextlib.suppress(OSError):
                leftover.unlink()
        self.upload_url = cloud.upload_profile(self.profile)

    def _run_base(self) -> dict[str, Any]:
        """What the profile's "run" section says about the run before it ends."""
        return {
            "id": self.run_dir.name,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "argv": self.argv,
            "runner": self.runner,
            "flakytest_version": __version__,
            "python": python_info(),
            "platform": platform_info(),
        }

    def _facts(self) -> dict[str, Any]:
        """The environment and git facts, also saved for a report of the run if it never finishes."""
        facts = {"environment": detect_environment(), "git": get_git_data()}
        with contextlib.suppress(OSError):
            write_json(self.run_dir / RUN_FILE, self._run_base() | facts)
        return facts

    def full_context(self) -> dict[str, Any]:
        """What the insights need to know about the test runner and the project (Django settings)."""
        django = _django_settings()
        if django and self.django_debug is not None:
            django["debug"] = self.django_debug
        return {"python_version": list(sys.version_info[:2]), "runner": self.runner, **self.context, "django": django}

    def summary_lines(self) -> list[str]:
        """The terminal summary, compared with the latest earlier run of the same tests, else the run before."""
        if self.profile is None or self.report_path is None:
            return []
        previous = runs.previous_summary(self.run_dir, self.profile)
        return text.terminal_summary(self.profile, self.report_path, self.upload_url, previous)


def recover_part(
    run_dir: Path,
    meta: dict[str, Any],
    rootdir: Path,
    started: float,
    reports: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The part of a process that didn't build its own: the tests it recorded and the samples its profiler wrote.
    reports: what the test runner reported about each test, which is more complete than the records."""
    tests: dict[str, dict[str, Any]] = {}
    tests_file = run_dir / f"tests-{meta['pid']}.jsonl"
    if tests_file.exists():
        for line in tests_file.read_text().splitlines():
            with contextlib.suppress(ValueError):
                record = json.loads(line)
                tests.setdefault(record["nodeid"], {}).update(record)
    for record in tests.values():
        if (report := (reports or {}).get(record["nodeid"])) is not None:
            record.update(phases=report["phases"], duration=report["duration"], outcome=report["outcome"])
    if meta.get("sampled_seconds") is not None and meta.get("status") == "running":
        meta["sampled_seconds"] = max(0.0, last_written(run_dir, meta) - meta["started_at"])
    process = meta | {
        "crashed": True,
        "tests": [t for t in tests.values() if "key" in t],
        "fixture_users": {},
        "fixture_stats": [],
        "context": {},
    }
    return analysis.build_part(process, rootdir, started)


def last_written(run_dir: Path, meta: dict[str, Any]) -> float:
    """When a process, or its profiler, last wrote to the run directory: about when it stopped."""
    paths = [run_dir / f"tests-{meta['pid']}.jsonl", Path(meta["samples_file"]) if meta.get("samples_file") else None]
    return max((path.stat().st_mtime for path in paths if path is not None and path.exists()), default=time.time())


def describe_run(
    base: dict[str, Any], parts: list[dict[str, Any]], rate: int, fallback_context: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """The profile's "run" section, from what the run knew (base) and what its processes' parts say, and the
    context the insights need."""
    sampled = [p for p in parts if p["backend"] != "none"]
    errors = [p["backend_error"] for p in parts if p.get("backend_error")]
    contexts = [p["context"] for p in parts if p.get("context")] or [fallback_context]
    context = next((c for c in contexts if c.get("django")), contexts[0])
    run = base | {
        "profiler": {
            "backend": sampled[0]["backend"] if sampled else "none",
            "rate": sampled[0]["rate"] if sampled else rate,
            "sampling_rate": (sampled[0].get("sampling_rate") or sampled[0]["rate"]) if sampled else rate,
            "samples": 0,
            "error": errors[0] if errors else None,
        },
        "workers": max(1, len([p for p in parts if p.get("worker")])),
        "plugins": sorted((context.get("pytest") or {}).get("plugins", [])),
    }
    return run, context


def write_outputs(profile: dict[str, Any], run_dir: Path) -> Path:
    """profile.json, the HTML report and summary.json. Returns the report's path."""
    profile_json = json.dumps(profile, separators=(",", ":"), default=str)
    (run_dir / "profile.json").write_text(profile_json)
    report_path = html.write_report(profile_json, run_dir)
    del profile_json
    runs.write_summary(profile, run_dir)
    return report_path


def crash_warning(
    profile: dict[str, Any],
    crashed: dict[str, str],
    recovered: list[str | None],
    missing: int = 0,
    *,
    unfinished: bool = False,
) -> dict[str, Any]:
    """Which processes didn't finish and why, what of theirs is in the profile, and whether they probably ran out of
    memory."""
    stopped = ", ".join(f"{worker} ({reason})" for worker, reason in sorted(crashed.items()))
    message = ""
    if unfinished:
        message = (
            f"This run didn't finish: the report is built from what its processes recorded "
            f"({len(profile['tests']):,} tests), without the tests that were running or still to run. "
        )
        if stopped:
            message += f"Processes that didn't finish: {stopped}. "
    elif stopped:
        message = f"Workers crashed: {stopped}. "
    if recovered and not unfinished:
        message += f"What {', '.join(str(w) for w in recovered)} recorded before crashing is included. "
    if missing:
        message += (
            f"{missing} test{'s have' if missing != 1 else ' has'} only exact durations and outcomes, from "
            "the test runner's reports: no samples, fixtures, queries or memory. "
        )
    if not missing and recovered and not unfinished:
        message += "Every test is in the profile. "
    message += out_of_memory_note(profile)
    return {"code": "missing_workers", "message": message.strip(), "workers": sorted(crashed)}


def out_of_memory_note(profile: dict[str, Any]) -> str:
    """Why processes probably died: together they needed about as much memory as the machine (or container) has."""
    total = profile["run"]["platform"].get("memory")
    usage = profile["summary"].get("memory") or {}
    together, each = usage.get("machine_peak"), usage.get("peak")
    if not total or not together or not each or together < OUT_OF_MEMORY_SHARE * total:
        return ""
    option = "--parallel" if profile["run"].get("runner") == "django" else "-n"
    fits = max(1, int(OUT_OF_MEMORY_SHARE * total // each))
    return (
        f"At their peak the tests needed {mb(together)} together, close to the {mb(total)} this machine has, so "
        f"processes were probably killed for running out of memory. At up to {mb(each)} per worker, {option} {fits} "
        "would fit."
    )


def build_unfinished(run_dir: Path, rootdir: Path) -> dict[str, Any]:
    """The profile of a run that never finished (its workers ran out of memory and pytest-xdist hung, or it was
    killed), from what its processes recorded, marked unfinished. The files it's built from stay, so it can be built
    again."""
    metas = [json.loads(path.read_text()) for path in sorted(run_dir.glob("meta-*.json"))]
    run_file = run_dir / RUN_FILE
    base: dict[str, Any] = json.loads(run_file.read_text()) if run_file.exists() else {}
    if "started_at" in base:
        started = dt.datetime.fromisoformat(base["started_at"].replace("Z", "+00:00")).timestamp()
    else:
        started = min((m["started_at"] for m in metas), default=time.time())
    parts = []
    stopped: dict[str, str] = {}
    for meta in metas:
        part_path = run_dir / f"part-{meta['pid']}.json"
        if part_path.exists():
            parts.append(json.loads(part_path.read_text()))
            continue
        parts.append(recover_part(run_dir, meta, rootdir, started))
        reason = "still running" if _running(meta["pid"]) else "exited without writing its profile"
        stopped[meta["worker"] or "main"] = reason
    ended = max((last_written(run_dir, meta) for meta in metas), default=started)
    base = (
        {
            "id": run_dir.name,
            "started_at": dt.datetime.fromtimestamp(started, dt.UTC).isoformat().replace("+00:00", "Z"),
            "argv": [],
            "runner": "pytest",
            "flakytest_version": __version__,
            "python": python_info(),
            "platform": platform_info(),
            "environment": detect_environment(),
            "git": {},
        }
        | base
        | {"duration": round(max(0.0, ended - started), 6), "exit_status": "UNFINISHED", "unfinished": True}
    )
    run, context = describe_run(base, parts, parts[0]["rate"] if parts else 0, {})
    profile = analysis.merge(run, parts, rootdir, context)
    profile["warnings"].insert(0, crash_warning(profile, stopped, [], unfinished=True))
    write_outputs(profile, run_dir)
    return profile


def _call(fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    return fn(*args, **kwargs)


class Background:
    """A function's result, worked out in a daemon thread."""

    def __init__(self, fn: Any) -> None:
        self.fn = fn
        self.value: Any = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="flakytest-facts", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        try:
            self.value = self.fn()
        except BaseException as e:  # re-raised where the result is needed
            self.error = e

    def result(self) -> Any:
        self.thread.join()
        if self.error is not None:
            raise self.error
        return self.value


def _running(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _django_debug() -> bool | None:
    conf = sys.modules.get("django.conf")
    if conf is None:
        return None
    try:
        return bool(conf.settings.DEBUG) if conf.settings.configured else None
    except Exception:
        return None


def _django_settings() -> dict[str, Any]:
    conf = sys.modules.get("django.conf")
    if conf is None:
        return {}
    try:
        settings = conf.settings
        if not settings.configured:
            return {}
        return {
            "password_hashers": list(getattr(settings, "PASSWORD_HASHERS", [])),
            "databases": sorted({db.get("ENGINE", "") for db in settings.DATABASES.values()}),
            "cache_backends": sorted({cache.get("BACKEND", "") for cache in getattr(settings, "CACHES", {}).values()}),
            "debug_toolbar": "debug_toolbar" in getattr(settings, "INSTALLED_APPS", []),
            "celery_eager": getattr(settings, "CELERY_TASK_ALWAYS_EAGER", None),
            "debug": getattr(settings, "DEBUG", None),
        }
    except Exception:
        return {}
