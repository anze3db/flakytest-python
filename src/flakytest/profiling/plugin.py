"""The pytest side of profiling: start the sampler, mark every test phase, record exact timings, and build
the profile and report when the session ends."""

from __future__ import annotations  # pytest 6 has no pytest.Parser, pytest.Config, … to annotate with

import contextlib
import datetime as dt
import importlib.util
import json
import os
import sys
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from flakytest import cloud
from flakytest.__about__ import __version__
from flakytest.environment import detect_environment, get_git_data, platform_info, python_info
from flakytest.profiling import analysis
from flakytest.profiling.dbtrack import QueryStats, QueryTracker
from flakytest.profiling.markers import PhaseMarkers, marker_key
from flakytest.profiling.samplers import BACKENDS, DEFAULT_RATES, Sampler, choose_backend
from flakytest.report import html, runs, text

TRUTHY = ("1", "true", "yes", "on")
WORKER_WAIT = 30.0


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
        help="Samples per second (default: 1000 for tachyon, 500 for py-spy). Raise it when the report says "
        "tests were undersampled.",
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


class ProfilerPlugin:
    """One per pytest process. Under pytest-xdist the controller only collects the workers' results."""

    active: "ProfilerPlugin | None" = None

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
        self.rate = _option(config, "flakytest_rate") or DEFAULT_RATES.get(self.backend, 1000)
        self.sampler: Sampler | None = None
        self.markers = PhaseMarkers()
        self.queries = QueryTracker()
        self.tests: dict[str, dict[str, Any]] = {}
        self.current: dict[str, Any] | None = None
        self.fixture_users: Counter[str] = Counter()
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

    def start(self) -> None:
        ProfilerPlugin.active = self
        if self.backend != "none":
            self.sampler = Sampler(self.backend, self.rate, self.run_dir)
            if not self.sampler.start():
                self.backend_error = self.sampler.error
                self.backend = "none"
                self.sampler = None
        self.markers.install()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_runtest_protocol(self, item: pytest.Item):
        nodeid = item.nodeid
        cls = getattr(item, "cls", None)
        record = self.tests[nodeid] = {
            "nodeid": nodeid,
            "key": marker_key(nodeid),
            "file": item.location[0],
            "class": cls.__name__ if cls is not None else None,
            "name": getattr(item, "name", nodeid.rsplit("::", 1)[-1]),
            "outcome": "passed",
            "worker": self.worker,
            "duration": 0.0,
            "phases": {},
            "fixtures": [],
            "longrepr": None,
        }
        self.fixture_users.update(getattr(item, "fixturenames", ()))
        self.queries.install()
        stats = QueryStats()
        self.current = record
        self.queries.current = stats
        self.markers.current_key = record["key"]
        try:
            yield
        finally:
            self.markers.current_key = None
            self.queries.current = None
            self.current = None
            if stats.queries:
                record["db"] = stats.as_dict()

    @pytest.hookimpl(hookwrapper=True)
    def pytest_fixture_setup(self, fixturedef: Any):
        started = time.perf_counter()
        yield
        record = self.current
        if record is not None:
            record["fixtures"].append(
                {
                    "name": fixturedef.argname,
                    "scope": fixturedef.scope,
                    "duration": round(time.perf_counter() - started, 6),
                }
            )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        record = self.tests.get(report.nodeid)
        if record is None or report.when not in ("setup", "call", "teardown"):
            return
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

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.markers.uninstall()
        samples_file = None
        sampled_seconds = 0.0
        if self.sampler is not None:
            samples_file = self.sampler.stop()
            sampled_seconds = self.sampler.stopped_at - self.sampler.started_at
            if samples_file is None:
                self.backend_error = self.sampler.error
        meta = {
            "pid": os.getpid(),
            "worker": self.worker,
            "backend": self.backend,
            "rate": self.rate,
            "backend_error": self.backend_error,
            "samples_file": str(samples_file) if samples_file else None,
            "sampled_seconds": sampled_seconds,
            "tests": list(self.tests.values()),
            "fixture_users": dict(self.fixture_users),
            "context": self._context(),
        }
        meta_path = self.run_dir / f"meta-{os.getpid()}.json"
        tmp = meta_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(meta, default=str))
        tmp.replace(meta_path)
        if self.worker:
            return
        try:
            self._build(exitstatus)
        except Exception as e:  # never fail the test run because the report couldn't be built
            sys.stderr.write(f"\nflakytest: could not build the profile: {type(e).__name__}: {e}\n")
        finally:
            ProfilerPlugin.active = None
            if self.controller:
                os.environ.pop("FLAKYTEST_RUN_DIR", None)

    def _processes(self) -> list[dict[str, Any]]:
        expected = getattr(self.config.option, "numprocesses", None) if self.controller else None
        deadline = time.monotonic() + WORKER_WAIT
        while True:
            metas = sorted(self.run_dir.glob("meta-*.json"))
            workers = [m for m in metas if m.name != f"meta-{os.getpid()}.json"]
            if not self.controller or not isinstance(expected, int) or len(workers) >= expected:
                break
            if time.monotonic() > deadline:
                break
            time.sleep(0.1)
        return [json.loads(path.read_text()) for path in metas]

    def _build(self, exitstatus: int) -> None:
        processes = self._processes()
        sampled = [p for p in processes if p["backend"] != "none"]
        backend = sampled[0]["backend"] if sampled else "none"
        rate = sampled[0]["rate"] if sampled else self.rate
        errors = [p["backend_error"] for p in processes if p.get("backend_error")]
        context = next((p["context"] for p in processes if p["context"].get("django")), processes[0]["context"])
        run = {
            "id": self.run_dir.name,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "duration": round(time.perf_counter() - self.started, 6),
            "exit_status": _exit_status_name(exitstatus),
            "argv": list(self.config.invocation_params.args),
            "flakytest_version": __version__,
            "python": python_info(),
            "platform": platform_info(),
            "environment": detect_environment(),
            "git": get_git_data(),
            "profiler": {
                "backend": backend,
                "rate": rate,
                "samples": 0,
                "error": errors[0] if errors else None,
            },
            "workers": max(1, len([p for p in processes if p.get("worker")])),
            "plugins": sorted(context["pytest"]["plugins"]),
        }
        rootdir = Path(str(self.config.rootpath if hasattr(self.config, "rootpath") else "."))
        self.profile = analysis.build_profile(run, processes, rootdir, context)
        (self.run_dir / "profile.json").write_text(json.dumps(self.profile, separators=(",", ":")))
        self.report_path = html.write_report(self.profile, self.run_dir)
        runs.mark_latest(self.run_dir)
        for leftover in [
            *self.run_dir.glob("meta-*.json"),
            *self.run_dir.glob("samples-*"),
            *self.run_dir.glob("*.ready"),
        ]:
            with contextlib.suppress(OSError):
                leftover.unlink()
        self.upload_url = cloud.upload_profile(self.profile)

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
            "django": _django_settings(),
        }

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        if self.profile is None or self.report_path is None:
            return
        write = terminalreporter.write_line
        terminalreporter.write_sep("=", "flakytest profile", cyan=True)
        for line in text.terminal_summary(self.profile, self.report_path, self.upload_url):
            write(line)


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
