"""The pytest side of profiling: a Session (session.py) driven by pytest's hooks. pytest's reports give each phase's
exact duration and outcome, and its fixture hook times fixtures.

Under pytest-xdist every worker has a session that profiles its tests, and the controller's session only merges
their parts, filling in a crashed worker's tests from the reports it got.
"""

from __future__ import annotations  # pytest 6 has no pytest.Parser, pytest.Config, … to annotate with

import contextlib
import importlib.util
import inspect
import os
import time
from pathlib import Path
from typing import Any

import pluggy
import pytest

from flakytest.profiling.samplers import BACKENDS
from flakytest.profiling.session import (
    BUSY_PY_SPY_RATE,
    LARGE_SUITE,
    LARGE_SUITE_RATE,
    Session,
    inherited_from,
    new_record,
)

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


# Options that make pytest print something and exit without running tests: nothing to profile
INFO_OPTIONS = ("help", "version", "markers", "showfixtures", "show_fixtures_per_test")


def runs_tests(config: pytest.Config) -> bool:
    return not any(_option(config, name) for name in INFO_OPTIONS)


def is_xdist_controller(config: pytest.Config) -> bool:
    if os.environ.get("PYTEST_XDIST_WORKER"):
        return False
    namespace = getattr(config, "known_args_namespace", None)
    workers = getattr(namespace, "numprocesses", None) if namespace is not None else None
    if workers is None:
        with contextlib.suppress(ValueError):
            workers = config.getoption("numprocesses")
    return bool(workers) and workers != "0" and not getattr(namespace, "collectonly", False)


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


class ProfilerPlugin:
    """One per pytest process."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        rootdir = Path(str(config.rootpath if hasattr(config, "rootpath") else "."))
        self.session = Session(
            rootdir,
            runner="pytest",
            argv=list(config.invocation_params.args),
            output=_option(config, "flakytest_output"),
            backend=_option(config, "flakytest_backend") or "auto",
            rate=_option(config, "flakytest_rate"),
            worker=os.environ.get("PYTEST_XDIST_WORKER"),
            controller=is_xdist_controller(config),
            workers=int(os.environ.get("PYTEST_XDIST_WORKER_COUNT") or 0),
        )

    def start(self) -> None:
        self.session.start()

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.session.collected(len(session.items))

    @pytest.hookimpl(**WRAPPER)  # type: ignore[call-overload]
    def pytest_runtest_protocol(self, item: pytest.Item):
        nodeid = item.nodeid
        cls = getattr(item, "cls", None)
        record = self.session.begin_test(
            nodeid,
            nodeid.partition("::")[0],  # like item.location[0] (relative to the rootdir), which costs ~60 µs
            cls.__name__ if cls is not None else None,
            getattr(item, "name", nodeid.rsplit("::", 1)[-1]),
            inherited_from(cls, getattr(item, "function", None)),
        )
        self.session.fixture_users.update(getattr(item, "fixturenames", ()))
        try:
            return (yield)
        finally:
            self.session.end_test(record)

    @pytest.hookimpl(**WRAPPER)  # type: ignore[call-overload]
    def pytest_fixture_setup(self, fixturedef: Any):
        token = self.session.fixture_started(shared=fixturedef.scope != "function")
        try:
            return (yield)
        finally:
            self.session.fixture_finished(token, fixturedef.argname, fixturedef.scope)

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        if report.when not in ("setup", "call", "teardown"):
            return
        session = self.session
        if session.controller:  # its tests run in the workers: the last report is when they finished
            session.last_report = time.perf_counter()
            record = session.reports.get(report.nodeid)
            if record is None:
                file, _, rest = report.nodeid.partition("::")
                parts = rest.split("::")
                node = getattr(report, "node", None)
                worker = getattr(getattr(node, "gateway", None), "id", None)
                record = session.reports[report.nodeid] = new_record(
                    report.nodeid, report.location[0] if report.location else file, None, parts[-1], worker
                )
                record["class"] = parts[-2] if len(parts) > 1 else None
            record_report(record, report)
            return
        record = session.tests.get(report.nodeid)
        if record is not None:
            record_report(record, report)

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node: Any, error: Any) -> None:
        """pytest-xdist: a worker finished, or crashed (error is set)."""
        if error:
            worker = getattr(getattr(node, "gateway", None), "id", None) or "?"
            self.session.crashed[worker] = str(error).strip().splitlines()[0] if str(error).strip() else "crashed"

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.session.finish(_exit_status_name(exitstatus), self._context())

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
            "pytest": {
                "plugins": sorted(set(plugins)),
                "pytest_django": pm.has_plugin("django"),
                "reuse_db": _option(self.config, "reuse_db"),
                "nomigrations": _option(self.config, "nomigrations"),
                "xdist_installed": importlib.util.find_spec("xdist") is not None,
                "cov": pm.has_plugin("_cov"),
                "coverage_core": os.environ.get("COVERAGE_CORE"),
                "testpaths": _testpaths(self.config),
            },
        }

    def pytest_terminal_summary(self, terminalreporter: Any) -> None:
        lines = self.session.summary_lines()
        if not lines:
            return
        terminalreporter.write_sep("=", "flakytest profile", cyan=True)
        for line in lines:
            terminalreporter.write_line(line)


def _option(config: pytest.Config, name: str) -> Any:
    namespace = getattr(config, "known_args_namespace", None)
    value = getattr(namespace, name, None) if namespace is not None else None
    if value is None:
        with contextlib.suppress(ValueError, AttributeError):
            value = config.getoption(name)
    return value


def _testpaths(config: pytest.Config) -> list[str]:
    with contextlib.suppress(ValueError):
        return [str(path) for path in config.getini("testpaths")]
    return []


def _exit_status_name(exitstatus: int) -> str:
    try:
        return pytest.ExitCode(exitstatus).name
    except ValueError:
        return str(exitstatus)
