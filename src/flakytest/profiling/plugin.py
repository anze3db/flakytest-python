"""The pytest side of profiling: start the sampler, mark every test phase, record exact timings, and build
the profile and report when the session ends.

Every test process records its tests as they finish (tests-<pid>.jsonl) and builds its part of the profile when its
session ends (part-<pid>.json). Under pytest-xdist the workers do that in parallel; the controller merges the parts,
and fills in the tests of a worker that crashed from what it recorded, or else from pytest's own reports.
"""

from __future__ import annotations  # pytest 6 has no pytest.Parser, pytest.Config, … to annotate with

import contextlib
import datetime as dt
import importlib.util
import inspect
import json
import os
import sys
import threading
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pluggy
import pytest

from flakytest import cloud
from flakytest.__about__ import __version__
from flakytest.environment import detect_environment, get_git_data, platform_info, python_info
from flakytest.profiling import analysis
from flakytest.profiling.dbtrack import QueryStats, QueryTracker
from flakytest.profiling.fixtures import SLOW_FIXTURE, FixtureStats
from flakytest.profiling.markers import PhaseMarkers, marker_key
from flakytest.profiling.memory import MemoryMeter
from flakytest.profiling.samplers import BACKENDS, DEFAULT_RATES, Sampler, choose_backend
from flakytest.report import html, runs, text

TRUTHY = ("1", "true", "yes", "on")
WORKER_WAIT = 30.0
SAMPLER_WAIT = 30.0
# Big suites are sampled less often (unless --flakytest-rate says otherwise): suite-wide insights don't need more,
# and every sample slows the tests down a little
LARGE_SUITE = 10_000
LARGE_SUITE_RATE = 500
BUSY_PY_SPY_RATE = 100
SAVE_EVERY = 1.0  # seconds between writing the records of finished tests to disk
# New-style hook wrappers (pluggy 1.1+) cost less per call than the old style, which pytest 6 still needs. Both
# run the same code: `return (yield)` hands the result on in the new style and is ignored in the old.
WRAPPER = (
    {"wrapper": True}
    if "wrapper" in inspect.signature(pluggy.HookimplMarker.__call__).parameters
    else {"hookwrapper": True}
)


def add_options(parser: pytest.Parser) -> None:
    group = parser.getgroup("flakytest", "flakytest profiling")
    group.addoption(
        "--flakytest-profile",
        action="store_true",
        default=os.environ.get("FLAKYTEST_PROFILE", "").lower() in TRUTHY,
        help="Profile every test with a sampling profiler and write a report to .flakytest/ "
        "(also enabled by FLAKYTEST_PROFILE=1).",
    )
    group.addoption(
        "--flakytest-rate",
        type=int,
        default=int(os.environ.get("FLAKYTEST_PROFILE_RATE") or 0) or None,
        help=f"Samples per second (default: 1000 for tachyon, {LARGE_SUITE_RATE} with more than "
        f"{LARGE_SUITE:,} tests; 500 for py-spy, {BUSY_PY_SPY_RATE} when pytest-xdist workers use every CPU). "
        "Raise it when the report says tests were undersampled.",
    )
    group.addoption(
        "--flakytest-backend",
        choices=BACKENDS,
        default=os.environ.get("FLAKYTEST_PROFILE_BACKEND", "auto"),
        help="Sampling profiler: tachyon (Python 3.15+), py-spy, none (timings only) or auto (default).",
    )
    group.addoption(
        "--flakytest-output",
        default=os.environ.get("FLAKYTEST_PROFILE_DIR"),
        help="Directory for profiling runs (default: <rootdir>/.flakytest/runs).",
    )


def profiling_requested(config: pytest.Config) -> bool:
    namespace = getattr(config, "known_args_namespace", None)
    if namespace is not None and getattr(namespace, "flakytest_profile", False):
        return True
    try:
        return bool(config.getoption("flakytest_profile"))
    except ValueError:
        return False


def is_xdist_controller(config: pytest.Config) -> bool:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return False
    namespace = getattr(config, "known_args_namespace", None)
    workers = getattr(namespace, "numprocesses", None) if namespace is not None else None
    if workers is None:
        with contextlib.suppress(ValueError):
            workers = config.getoption("numprocesses")
    return bool(workers) and workers != "0" and not getattr(namespace, "collectonly", False)


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


def record_report(record: dict[str, Any], report: pytest.TestReport) -> None:
    """Add a setup/call/teardown report to a test's record: its duration and outcome."""
    record["phases"][report.when] = report.duration
    record["duration"] = sum(record["phases"].values())
    outcome = str(report.outcome).removeprefix("mute-")
    xfail = hasattr(report, "wasxfail")
    if report.when == "setup":
        if outcome == "failed":
            record["outcome"] = "error"
        elif outcome == "skipped":
            record["outcome"] = "xfailed" if xfail else "skipped"
    elif report.when == "call":
        if outcome == "passed":
            record["outcome"] = "xpassed" if xfail else "passed"
        elif outcome == "skipped":
            record["outcome"] = "xfailed" if xfail else "skipped"
        else:
            record["outcome"] = "failed"
    elif outcome == "failed" and record["outcome"] in ("passed", "xpassed"):
        record["outcome"] = "error"
    if outcome == "failed" and report.longrepr and not record["longrepr"]:
        record["longrepr"] = str(report.longrepr)


def write_json(path: Path, data: Any) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, separators=(",", ":"), default=str))
    tmp.replace(path)


class ProfilerPlugin:
    """One per pytest process. Under pytest-xdist the controller only collects the workers' results."""

    active: ProfilerPlugin | None = None

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.started_at = dt.datetime.now(dt.UTC)
        self.started = time.perf_counter()
        self.worker = os.environ.get("PYTEST_XDIST_WORKER")
        self.controller = is_xdist_controller(config)
        self.run_dir = self._run_dir()
        requested = _option(config, "flakytest_backend") or "auto"
        self.backend, self.backend_error = choose_backend(requested)
        if self.controller:
            self.backend, self.backend_error = "none", None
        self.explicit_rate = _option(config, "flakytest_rate")
        self.rate = self.explicit_rate or DEFAULT_RATES.get(self.backend, 1000)
        workers = int(os.environ.get("PYTEST_XDIST_WORKER_COUNT") or 0)
        if self.backend == "py-spy" and not self.explicit_rate and workers >= (os.cpu_count() or 1):
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
        # The last test, until the next one starts: what it kept is measured then
        self.pending: tuple[dict[str, Any], dict[str, Any] | None] | None = None
        self.classes_seen: set[tuple[str, str | None]] = set()
        self.fixture_users: Counter[str] = Counter()
        self.fixture_stats = FixtureStats()
        self.django_debug: bool | None = None
        self.tests_file: Any = None
        self.saved_at = 0.0
        self.facts: Background | None = None
        # The controller's view of every test, from the reports workers send, and the workers that crashed
        self.reports: dict[str, dict[str, Any]] = {}
        self.crashed: dict[str, str] = {}
        self.last_report = self.started
        self.profile: dict[str, Any] | None = None
        self.report_path: Path | None = None
        self.upload_url: str | None = None

    def _run_dir(self) -> Path:
        shared = os.environ.get("FLAKYTEST_RUN_DIR")
        if shared and self.worker:
            return Path(shared)
        base = _option(self.config, "flakytest_output")
        root = Path(base) if base else Path(self.config.rootpath if hasattr(self.config, "rootpath") else ".")
        runs_dir = root if base else runs.runs_dir(root)
        run_dir = runs_dir / f"{self.started_at:%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}"
        run_dir.mkdir(parents=True, exist_ok=True)
        runs.ignore_in_git(runs_dir)
        if self.controller:
            # pytest-xdist workers inherit the environment, so they write into the same directory
            os.environ["FLAKYTEST_RUN_DIR"] = str(run_dir)
        return run_dir

    @property
    def pid(self) -> int:
        return os.getpid()

    def start(self) -> None:
        ProfilerPlugin.active = self
        if self.backend != "none":
            self.sampler = Sampler(self.backend, self.rate, self.run_dir)
            if not self.sampler.start():
                self.backend_error = self.sampler.error
                self.backend = "none"
                self.sampler = None
        if self.sampler is not None:  # only samples need to know which test they were taken in
            self.markers.install()
        if self.worker:  # only the controller can use what a crashed process recorded
            self.tests_file = (self.run_dir / f"tests-{self.pid}.jsonl").open("a", encoding="utf-8")
        if not self.controller:
            self._write_meta("running")
        if not self.worker:  # git runs a few subprocesses: let them run while the tests do
            self.facts = Background(lambda: {"environment": detect_environment(), "git": get_git_data()})

    def _process(self, status: str) -> dict[str, Any]:
        sampler = self.sampler
        return {
            "pid": self.pid,
            "status": status,
            "worker": self.worker,
            "backend": self.backend,
            "rate": self.rate,
            "backend_error": self.backend_error,
            "samples_file": str(sampler.out) if sampler else None,
            "sampler_pid": sampler.process.pid if sampler and sampler.process else None,
            "sampled_seconds": (sampler.stopped_at or time.perf_counter()) - sampler.started_at if sampler else 0.0,
            "memory_source": self.memory.source,
            "started_at": self.started_at.timestamp(),
        }

    def _write_meta(self, status: str, **extra: Any) -> None:
        write_json(self.run_dir / f"meta-{self.pid}.json", self._process(status) | extra)

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        if self.sampler is not None and not self.explicit_rate and len(session.items) > LARGE_SUITE:
            self.sampler.set_rate(LARGE_SUITE_RATE)

    @pytest.hookimpl(**WRAPPER)  # type: ignore[call-overload]
    def pytest_runtest_protocol(self, item: pytest.Item):
        nodeid = item.nodeid
        cls = getattr(item, "cls", None)
        record = new_record(
            nodeid,
            nodeid.partition("::")[0],  # like item.location[0] (relative to the rootdir), which costs ~60 µs
            cls.__name__ if cls is not None else None,
            getattr(item, "name", nodeid.rsplit("::", 1)[-1]),
            self.worker,
        )
        self.tests[nodeid] = record
        self.fixture_users.update(getattr(item, "fixturenames", ()))
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
        self.queries.current = stats
        self.markers.current_key = record["key"]
        try:
            return (yield)
        finally:
            self.markers.current_key = None
            self.queries.current = None
            self.current = None
            if stats.queries:
                record["db"] = stats.as_dict()
            self.pending = (record, self.current_memory)
            self.current_memory = None
            if self.django_debug is None and "call" in record["phases"]:
                # pytest-django sets DEBUG for the session in the first test's setup and restores it at the end
                self.django_debug = _django_debug()

    def _finish_pending(self, reading: dict[str, Any] | None) -> None:
        """Measure what the last test kept, and record it for good."""
        if self.pending is None:
            return
        record, started = self.pending
        self.pending = None
        if used := self.memory.stop(started, reading):
            if started and started.get("warmup"):
                used["warmup"] = started["warmup"]
            record["memory"] = used
        self._save(record)

    def _save(self, record: dict[str, Any]) -> None:
        """Append a finished test to this process's records, so they survive the process crashing: written at most
        a second later (pytest's own reports fill in the tests a crash loses)."""
        if self.tests_file is not None:
            with contextlib.suppress(OSError, ValueError):
                self.tests_file.write(json.dumps(record, separators=(",", ":"), default=str) + "\n")
                if (now := time.monotonic()) - self.saved_at >= SAVE_EVERY:
                    self.tests_file.flush()
                    self.saved_at = now

    @pytest.hookimpl(**WRAPPER)  # type: ignore[call-overload]
    def pytest_fixture_setup(self, fixturedef: Any):
        record = self.current
        shared = fixturedef.scope != "function"
        queries = previous_queries = None
        before = None
        if record is not None and shared:
            # Queries and memory of a fixture other tests share are the fixture's, not this test's
            queries, previous_queries = QueryStats(), self.queries.current
            self.queries.current = queries
            before = self.memory.read()
        started = time.perf_counter()
        try:
            return (yield)
        finally:
            duration = time.perf_counter() - started
            nodeid = record["nodeid"] if record is not None else None
            self.fixture_stats.add(fixturedef.argname, fixturedef.scope, duration, nodeid)
            if record is not None and (shared or duration >= SLOW_FIXTURE):
                entry: dict[str, Any] = {"name": fixturedef.argname, "scope": fixturedef.scope, "duration": r(duration)}
                record["fixtures"].append(entry)
                if shared:
                    self.queries.current = previous_queries
                    if queries is not None and queries.queries:
                        entry["db"] = queries.as_dict()
                    after = self.memory.read()
                    if before and after and before.get("private") is not None and after.get("private") is not None:
                        entry["memory"] = after["private"] - before["private"]
                    self.current_memory = self.memory.restart(self.current_memory)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in ("setup", "call", "teardown"):
            return
        self.last_report = time.perf_counter()
        if self.controller:
            record = self.reports.get(report.nodeid)
            if record is None:
                file, _, rest = report.nodeid.partition("::")
                parts = rest.split("::")
                node = getattr(report, "node", None)
                worker = getattr(getattr(node, "gateway", None), "id", None)
                record = self.reports[report.nodeid] = new_record(
                    report.nodeid, report.location[0] if report.location else file, None, parts[-1], worker
                )
                record["class"] = parts[-2] if len(parts) > 1 else None
            record_report(record, report)
            return
        record = self.tests.get(report.nodeid)
        if record is not None:
            record_report(record, report)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        """pytest-xdist: a worker finished, or crashed (error is set)."""
        if error:
            worker = getattr(getattr(node, "gateway", None), "id", None) or "?"
            self.crashed[worker] = str(error).strip().splitlines()[0] if str(error).strip() else "crashed"

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.markers.uninstall()
        try:
            if not self.controller:
                self._finish_pending(self.memory.read())
                self._build_part()
            if not self.worker:
                self._build(exitstatus)
        except Exception as e:  # never fail the test run because the report couldn't be built
            sys.stderr.write(f"\nflakytest: could not build the profile: {type(e).__name__}: {e}\n")
            if self.worker:  # don't keep the controller waiting for this worker's part
                with contextlib.suppress(OSError):
                    self._write_meta("failed")
        finally:
            if self.tests_file is not None:
                with contextlib.suppress(OSError):
                    self.tests_file.close()
            if not self.worker:
                ProfilerPlugin.active = None
            if self.controller:
                os.environ.pop("FLAKYTEST_RUN_DIR", None)

    def _build_part(self) -> None:
        """This process's part of the profile (under pytest-xdist: built in every worker at once)."""
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
            "context": self._context(),
        }
        part = analysis.build_part(process, self._rootdir(), self.started_at.timestamp())
        part["build_seconds"] = round(time.perf_counter() - started, 3)
        write_json(self.run_dir / f"part-{self.pid}.json", part)
        self._write_meta("done")

    def _rootdir(self) -> Path:
        return Path(str(self.config.rootpath if hasattr(self.config, "rootpath") else "."))

    def _parts(self) -> list[dict[str, Any]]:
        """Every process's part. A worker that crashed left no part: build it from its records and samples."""
        expected = getattr(self.config.option, "numprocesses", None) if self.controller else None
        deadline = time.monotonic() + WORKER_WAIT
        while self.controller:
            metas = [json.loads(path.read_text()) for path in self.run_dir.glob("meta-*.json")]
            unfinished = [
                m
                for m in metas
                if m["status"] != "failed"
                and m["worker"] not in self.crashed
                and not (self.run_dir / f"part-{m['pid']}.json").exists()
            ]
            started = len(metas) >= expected if isinstance(expected, int) else True
            if (started and not unfinished) or time.monotonic() > deadline:
                break
            time.sleep(0.1)
        parts = []
        for path in sorted(self.run_dir.glob("meta-*.json")):
            meta = json.loads(path.read_text())
            part_path = self.run_dir / f"part-{meta['pid']}.json"
            if part_path.exists():
                parts.append(json.loads(part_path.read_text()))
            else:
                parts.append(self._recover(meta))
        return parts

    def _recover(self, meta: dict[str, Any]) -> dict[str, Any]:
        """The part of a worker that died before building it: its recorded tests, and the samples its profiler wrote
        (the profiler notices the worker is gone and writes what it has)."""
        sampler_pid = meta.get("sampler_pid")
        deadline = time.monotonic() + SAMPLER_WAIT
        while sampler_pid and _running(sampler_pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        tests: dict[str, dict[str, Any]] = {}
        tests_file = self.run_dir / f"tests-{meta['pid']}.jsonl"
        if tests_file.exists():
            for line in tests_file.read_text().splitlines():
                with contextlib.suppress(ValueError):
                    record = json.loads(line)
                    tests.setdefault(record["nodeid"], {}).update(record)
        for record in tests.values():  # the reports pytest-xdist got are the more complete
            if (report := self.reports.get(record["nodeid"])) is not None:
                record.update(phases=report["phases"], duration=report["duration"], outcome=report["outcome"])
        if meta.get("sampled_seconds") is not None and meta.get("status") == "running":
            meta["sampled_seconds"] = max(0.0, time.time() - meta["started_at"])
        process = meta | {
            "tests": [t for t in tests.values() if "key" in t],
            "fixture_users": {},
            "fixture_stats": [],
            "context": {},
        }
        return analysis.build_part(process, self._rootdir(), self.started_at.timestamp())

    def _backfill(self, parts: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, int]:
        """A part for the tests pytest reported that no process recorded: exact timings and outcomes only."""
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
        return analysis.build_part(process, self._rootdir(), self.started_at.timestamp()), len(missing)

    def _build(self, exitstatus: int) -> None:
        started = time.perf_counter()
        parts = self._parts()
        backfill, missing = self._backfill(parts) if self.controller else (None, 0)
        recovered = [p.get("worker") for p in parts if p.get("worker") in self.crashed]
        if backfill is not None:
            parts.append(backfill)
        sampled = [p for p in parts if p["backend"] != "none"]
        backend = sampled[0]["backend"] if sampled else "none"
        rate = sampled[0]["rate"] if sampled else self.rate
        errors = [p["backend_error"] for p in parts if p.get("backend_error")]
        contexts = [p["context"] for p in parts if p.get("context")] or [self._context()]
        context = next((c for c in contexts if c.get("django")), contexts[0])
        run = {
            "id": self.run_dir.name,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "duration": round(time.perf_counter() - self.started, 6),
            "exit_status": _exit_status_name(exitstatus),
            "argv": list(self.config.invocation_params.args),
            "flakytest_version": __version__,
            "python": python_info(),
            "platform": platform_info(),
            **(self.facts.result() if self.facts else {"environment": detect_environment(), "git": get_git_data()}),
            "profiler": {
                "backend": backend,
                "rate": rate,
                "samples": 0,
                "error": errors[0] if errors else None,
            },
            "workers": max(1, len([p for p in parts if p.get("worker")])),
            "plugins": sorted((context.get("pytest") or {}).get("plugins", [])),
        }
        self.profile = analysis.merge(run, parts, self._rootdir(), context)
        # How long building the profile took after the last test: the processes' parts (built at the same time
        # under pytest-xdist), then merging them. Writing the files comes on top.
        run["postprocessing"] = {
            "tests_finished_at": round(self.last_report - self.started, 3),  # seconds into the session
            "parts": max((p.get("build_seconds") or 0.0 for p in parts), default=0.0),
            "merge": round(time.perf_counter() - started, 3),
        }
        if self.crashed or missing:
            self.profile["warnings"].insert(0, self._missing_warning(missing, recovered))
        profile_json = json.dumps(self.profile, separators=(",", ":"), default=str)
        (self.run_dir / "profile.json").write_text(profile_json)
        self.report_path = html.write_report(profile_json, self.run_dir)
        del profile_json
        runs.write_summary(self.profile, self.run_dir)
        runs.mark_latest(self.run_dir)
        leftovers = [
            *self.run_dir.glob("meta-*.json"),
            *self.run_dir.glob("part-*.json"),
            *self.run_dir.glob("tests-*.jsonl"),
            *self.run_dir.glob("samples-*"),
            *self.run_dir.glob("control-*"),
            *self.run_dir.glob("*.ready"),
        ]
        if not errors and not self.crashed:  # keep the profiler's log when something went wrong
            leftovers += self.run_dir.glob("sampler-*.log")
        for leftover in leftovers:
            with contextlib.suppress(OSError):
                leftover.unlink()
        self.upload_url = cloud.upload_profile(self.profile)

    def _missing_warning(self, missing: int, recovered: list[str | None]) -> dict[str, Any]:
        crashed = ", ".join(f"{worker} ({reason})" for worker, reason in sorted(self.crashed.items()))
        message = f"pytest-xdist workers crashed: {crashed}. " if crashed else ""
        if recovered:
            message += f"What {', '.join(str(w) for w in recovered)} recorded before crashing is included. "
        if missing:
            message += (
                f"{missing} test{'s have' if missing != 1 else ' has'} only exact durations and outcomes, from "
                "pytest's reports: no samples, fixtures, queries or memory."
            )
        if not missing and recovered:
            message += "Every test is in the profile."
        return {"code": "missing_workers", "message": message.strip(), "workers": sorted(self.crashed)}

    def _context(self) -> dict[str, Any]:
        pm = self.config.pluginmanager
        plugins = [
            name
            for name, plugin_name in (
                ("django", "django"),
                ("xdist", "xdist.looponfail"),
                ("xdist", "dsession"),
                ("cov", "_cov"),
                ("asyncio", "asyncio"),
                ("randomly", "randomly"),
                ("rerunfailures", "rerunfailures"),
                ("timeout", "timeout"),
                ("hypothesis", "hypothesispytest"),
            )
            if pm.has_plugin(plugin_name)
        ]
        django = _django_settings()
        if django and self.django_debug is not None:
            django["debug"] = self.django_debug
        return {
            "python_version": list(sys.version_info[:2]),
            "pytest": {
                "plugins": sorted(set(plugins)),
                "pytest_django": pm.has_plugin("django"),
                "reuse_db": _option(self.config, "reuse_db"),
                "nomigrations": _option(self.config, "nomigrations"),
                "xdist_installed": importlib.util.find_spec("xdist") is not None,
                "cov": pm.has_plugin("_cov"),
                "coverage_core": os.environ.get("COVERAGE_CORE"),
            },
            "django": django,
        }

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.profile is None or self.report_path is None:
            return
        write = terminalreporter.write_line
        terminalreporter.write_sep("=", "flakytest profile", cyan=True)
        previous = None
        earlier = sorted(
            run
            for run in self.run_dir.parent.iterdir()
            if run.name < self.run_dir.name and (run / "profile.json").exists()
        )
        if earlier:
            with contextlib.suppress(OSError, ValueError, KeyError):
                previous = runs.load_summary(earlier[-1])
        for line in text.terminal_summary(self.profile, self.report_path, self.upload_url, previous):
            write(line)


def r(value: float) -> float:
    return round(value, 6)


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


def _running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _option(config: pytest.Config, name: str) -> Any:
    namespace = getattr(config, "known_args_namespace", None)
    value = getattr(namespace, name, None) if namespace is not None else None
    if value is None:
        with contextlib.suppress(ValueError, AttributeError):
            value = config.getoption(name)
    return value


def _exit_status_name(exitstatus: int) -> str:
    try:
        return pytest.ExitCode(exitstatus).name
    except ValueError:
        return str(exitstatus)


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
