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
from flakytest.profiling.memory import MemoryMeter
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
        if self.worker:  # only the process that started the workers can use what a crashed one recorded
            self.tests_file = (self.run_dir / f"tests-{self.pid}.jsonl").open("a", encoding="utf-8")
        if not self.controller:
            self._write_meta("running")
        if not self.worker:  # git runs a few subprocesses: let them run while the tests do
            self.facts = Background(lambda: {"environment": detect_environment(), "git": get_git_data()})

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
        for record in tests.values():  # the reports the parent got are the more complete
            if (report := self.reports.get(record["nodeid"])) is not None:
                record.update(phases=report["phases"], duration=report["duration"], outcome=report["outcome"])
        if meta.get("sampled_seconds") is not None and meta.get("status") == "running":
            meta["sampled_seconds"] = max(0.0, time.time() - meta["started_at"])
        process = meta | {
            "crashed": True,
            "tests": [t for t in tests.values() if "key" in t],
            "fixture_users": {},
            "fixture_stats": [],
            "context": {},
        }
        return analysis.build_part(process, self.rootdir, self.started_at.timestamp())

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
        sampled = [p for p in parts if p["backend"] != "none"]
        backend = sampled[0]["backend"] if sampled else "none"
        rate = sampled[0]["rate"] if sampled else self.rate
        errors = [p["backend_error"] for p in parts if p.get("backend_error")]
        contexts = [p["context"] for p in parts if p.get("context")] or [self.full_context()]
        context = next((c for c in contexts if c.get("django")), contexts[0])
        run = {
            "id": self.run_dir.name,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "duration": round(time.perf_counter() - self.started, 6),
            "exit_status": exit_status,
            "argv": self.argv,
            "runner": self.runner,
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
        self.profile = analysis.merge(run, parts, self.rootdir, context)
        # How long building the profile took after the last test: the processes' parts (built at the same time with
        # several workers), then merging them. Writing the files comes on top.
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
        message = f"Workers crashed: {crashed}. " if crashed else ""
        if recovered:
            message += f"What {', '.join(str(w) for w in recovered)} recorded before crashing is included. "
        if missing:
            message += (
                f"{missing} test{'s have' if missing != 1 else ' has'} only exact durations and outcomes, from "
                "the test runner's reports: no samples, fixtures, queries or memory."
            )
        if not missing and recovered:
            message += "Every test is in the profile."
        return {"code": "missing_workers", "message": message.strip(), "workers": sorted(self.crashed)}

    def full_context(self) -> dict[str, Any]:
        """What the insights need to know about the test runner and the project (Django settings)."""
        django = _django_settings()
        if django and self.django_debug is not None:
            django["debug"] = self.django_debug
        return {"python_version": list(sys.version_info[:2]), "runner": self.runner, **self.context, "django": django}

    def summary_lines(self) -> list[str]:
        """The terminal summary, compared with the run before this one."""
        if self.profile is None or self.report_path is None:
            return []
        previous = None
        earlier = sorted(
            run
            for run in self.run_dir.parent.iterdir()
            if run.name < self.run_dir.name and (run / "profile.json").exists()
        )
        if earlier:
            with contextlib.suppress(OSError, ValueError, KeyError):
                previous = runs.load_summary(earlier[-1])
        return text.terminal_summary(self.profile, self.report_path, self.upload_url, previous)


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
