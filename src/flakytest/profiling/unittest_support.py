"""Profile tests run by unittest or Django's test runner, without pytest:

    flakytest python -m unittest discover
    flakytest python manage.py test --parallel 4

The command runs in this process, like `python` would, after unittest is patched so a test's setup (Django's
transaction, setUp), call and teardown (tearDown, cleanups, Django's rollback) are timed and run inside marker frames,
and setUpClass / setUpModule (Django's setUpTestData included) are recorded as class and module fixtures that the
tests share. Django's --parallel workers each profile their tests and write their part; this process merges them.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import os
import runpy
import sys
import traceback
import unittest
from collections.abc import Callable
from multiprocessing import util
from pathlib import Path
from typing import Any

from flakytest.profiling.samplers import BACKENDS
from flakytest.profiling.session import Session

OPTIONS = {
    "--flakytest-rate": "FLAKYTEST_PROFILE_RATE",
    "--flakytest-backend": "FLAKYTEST_PROFILE_BACKEND",
    "--flakytest-output": "FLAKYTEST_PROFILE_DIR",
}
PHASE_METHODS = {
    "_callSetUp": "setup",
    "_callTestMethod": "call",
    "_callTearDown": "teardown",
    "doCleanups": "teardown",
}
DJANGO_PHASES = {"_pre_setup": "setup", "_post_teardown": "teardown"}

_originals: dict[tuple[type, str], Any] = {}
_running: set[int] = set()  # id() of the tests whose run this module is handling
_described: dict[type, tuple[str, str]] = {}  # test class -> (file, class name)
_django = {"patched": False}
_module_fixture: list[dict[str, Any]] = []  # the test whose module fixture is being set up
_runner_settings: dict[str, Any] = {}  # Django's test runner: keepdb, parallel


class UsageError(Exception):
    pass


def parse(args: list[str]) -> tuple[dict[str, str], str, str, list[str]]:
    """flakytest's own options, then what to run: ("module", name, args), ("script", path, args) or ("code", …)."""
    options: dict[str, str] = {}
    args = list(args)
    while args and args[0].startswith("--flakytest-"):
        name, _, value = args.pop(0).partition("=")
        if name not in OPTIONS:
            message = f"unknown option {name}"
            raise UsageError(message)
        if not value:
            if not args:
                message = f"{name} needs a value"
                raise UsageError(message)
            value = args.pop(0)
        if name == "--flakytest-backend" and value not in BACKENDS:
            message = f"--flakytest-backend must be one of {', '.join(BACKENDS)}"
            raise UsageError(message)
        options[name] = value
    if not args:
        message = "nothing to run: flakytest python -m unittest …, or flakytest python manage.py test …"
        raise UsageError(message)
    first = args.pop(0)
    if first == "-m" and args:
        return options, "module", args[0], args[1:]
    if first.startswith("-m") and len(first) > 2:
        return options, "module", first[2:], args
    if first == "-c" and args:
        return options, "code", args[0], args[1:]
    if first.startswith("-"):
        message = (
            f"flakytest python runs a script, -m module or -c code; set interpreter options like {first} in the "
            "environment instead (PYTHONWARNINGS, PYTHONDEVMODE, …)"
        )
        raise UsageError(message)
    return options, "script", first, args


def run(args: list[str]) -> int:
    """`flakytest python …`: run it with every unittest test profiled. Returns the exit status."""
    options, kind, target, rest = parse(args)
    for name, value in options.items():  # Django's parallel workers read them too
        os.environ[OPTIONS[name]] = value
    session = Session(
        Path.cwd(),
        runner="unittest",
        argv=args,
        output=os.environ.get("FLAKYTEST_PROFILE_DIR"),
        backend=os.environ.get("FLAKYTEST_PROFILE_BACKEND", "auto"),
        rate=int(os.environ.get("FLAKYTEST_PROFILE_RATE") or 0) or None,
    )
    session.start(markers=False)
    install()
    status = 0
    try:
        status = _run_python(kind, target, rest)
    except SystemExit as e:
        status = _exit_code(e.code)
    finally:
        session.runner = "django" if "django.test.runner" in sys.modules and _django["patched"] else "unittest"
        _end_current(session)
        session.finish("OK" if status == 0 else "TESTS_FAILED", _context())
        lines = session.summary_lines()
        if lines:
            sys.stderr.write("\n" + " flakytest profile ".center(70, "=") + "\n" + "\n".join(lines) + "\n")
            sys.stderr.flush()
    return status


def _exit_code(code: Any) -> int:
    if code is None:
        return 0
    if isinstance(code, int):
        return code
    sys.stderr.write(f"{code}\n")
    return 1


def _run_python(kind: str, target: str, args: list[str]) -> int:
    """Run a module, script or code the way `python` would, in this process."""
    if kind == "module":
        sys.argv = [target, *args]
        sys.path.insert(0, os.getcwd())
        runpy.run_module(target, run_name="__main__", alter_sys=True)
    elif kind == "script":
        sys.argv = [target, *args]
        sys.path.insert(0, str(Path(target).resolve().parent))
        runpy.run_path(target, run_name="__main__")
    else:
        sys.argv = ["-c", *args]
        sys.path.insert(0, "")
        exec(compile(target, "<string>", "exec"), {"__name__": "__main__"})  # noqa: S102
    return 0


def _context() -> dict[str, Any]:
    return {"unittest": dict(_runner_settings)}


def install() -> None:
    """Patch unittest (and Django's runner when it's imported). Safe to call again, and in a forked worker."""
    if _originals:
        return
    _patch(unittest.TestSuite, "_handleModuleFixture", _handle_module_fixture)
    _patch(unittest.TestSuite, "_handleClassSetUp", _handle_class_setup)
    _patch(unittest.TestSuite, "_tearDownPreviousClass", _teardown_previous_class)
    _patch(unittest.TestSuite, "_handleModuleTearDown", _teardown_module)
    _patch(unittest.TestCase, "run", _run_test)
    classes = [unittest.TestCase]
    if hasattr(unittest, "IsolatedAsyncioTestCase"):
        classes.append(unittest.IsolatedAsyncioTestCase)
    for cls in classes:
        for method, phase in PHASE_METHODS.items():
            if method in cls.__dict__:
                _patch(cls, method, _phase_method(phase, (cls, method)))
    if "django.test.runner" in sys.modules:
        _patch_django()
    else:
        sys.meta_path.insert(0, DjangoImportHook())


class DjangoImportHook:
    """Patches Django's test runner as soon as it's imported: its parallel workers start before any test runs."""

    def find_spec(self, name: str, path: Any, target: Any = None) -> Any:  # noqa: ARG002 (the finder protocol)
        if name != "django.test.runner":
            return None
        sys.meta_path.remove(self)
        spec = importlib.util.find_spec(name)
        if spec is None or spec.loader is None or not hasattr(spec.loader, "exec_module"):
            return spec
        exec_module = spec.loader.exec_module

        def exec_and_patch(module: Any) -> None:
            exec_module(module)
            _patch_django()

        spec.loader.exec_module = exec_and_patch  # type: ignore[method-assign]
        return spec


def _patch(cls: type, name: str, replacement: Callable[..., Any]) -> None:
    descriptor = cls.__dict__[name]
    _originals[(cls, name)] = descriptor
    if isinstance(descriptor, classmethod):
        setattr(cls, name, classmethod(replacement))
    else:
        setattr(cls, name, replacement)


def _original(cls: type, name: str) -> Callable[..., Any]:
    descriptor = _originals[(cls, name)]
    return descriptor.__func__ if isinstance(descriptor, classmethod) else descriptor


def _patch_django() -> None:
    """Once Django's test framework is imported: its per-test transaction, test databases and parallel workers."""
    if _django["patched"] or "django.test.runner" not in sys.modules:
        return
    _django["patched"] = True
    runner, testcases = django_module("runner"), django_module("testcases")

    _patch(testcases.SimpleTestCase, "_setup_and_call", _setup_and_call)
    for cls in (testcases.SimpleTestCase, testcases.TransactionTestCase, testcases.TestCase):
        for method, phase in DJANGO_PHASES.items():
            if method in cls.__dict__:
                _patch(cls, method, _phase_method(phase, (cls, method)))
    _patch(runner.DiscoverRunner, "setup_databases", _setup_databases)
    if hasattr(runner, "ParallelTestSuite") and "init_worker" in runner.ParallelTestSuite.__dict__:
        _originals[(runner.ParallelTestSuite, "init_worker")] = runner.ParallelTestSuite.__dict__["init_worker"]
        runner.ParallelTestSuite.init_worker = _init_worker


def django_module(name: str) -> Any:
    """django.test.<name>: Django is optional, so it's imported only once the project has."""
    return importlib.import_module(f"django.test.{name}")


def describe(test: unittest.TestCase) -> tuple[str, str, str | None, str]:
    """(node id, file, class, name), node ids like pytest's: tests/test_orders.py::TestCheckout::test_list."""
    cls = type(test)
    name = getattr(test, "_testMethodName", None)
    if name is None or not isinstance(test, unittest.TestCase) or type(test) is unittest.FunctionTestCase:
        return test.id(), cls.__module__.replace(".", "/") + ".py", None, test.id().rsplit(".", 1)[-1]
    described = _described.get(cls)
    if described is None:
        session = Session.active
        file = cls.__module__.replace(".", "/") + ".py"
        try:
            source = inspect.getsourcefile(cls)
            if source and session is not None:
                file = Path(os.path.relpath(source, session.rootdir)).as_posix()
        except (TypeError, OSError, ValueError):
            pass
        described = _described[cls] = (file, cls.__qualname__)
    file, class_name = described
    return f"{file}::{class_name}::{name}", file, class_name, name


def _begin(session: Session, test: Any) -> dict[str, Any]:
    nodeid, file, cls, name = describe(test)
    current = session.current
    if current is not None:
        if current["nodeid"] == nodeid:
            return current
        _end_current(session)
    return session.begin_test(nodeid, file, cls, name)


def _end_current(session: Session) -> None:
    """A test that the suite began but never ran (its setUpClass failed or skipped the class)."""
    record = session.current
    if record is None:
        return
    if "call" not in record["phases"] and record["outcome"] == "passed":
        record["outcome"] = "skipped"
    session.end_test(record)


def _handle_module_fixture(self: Any, test: Any, result: Any) -> Any:
    original = _original(unittest.TestSuite, "_handleModuleFixture")
    session = Session.active
    if session is None or not isinstance(test, unittest.TestCase):
        return original(self, test, result)
    record = _begin(session, test)
    if self._get_previous_module(result) == test.__class__.__module__:
        return original(self, test, result)
    # Changing modules tears down the previous one in here too: that's part of this module fixture
    _module_fixture.append(record)
    try:
        return _shared(session, record, "setUpModule", "module", original, self, test, result)
    finally:
        _module_fixture.pop()


def _handle_class_setup(self: Any, test: Any, result: Any) -> Any:
    original = _original(unittest.TestSuite, "_handleClassSetUp")
    session = Session.active
    record = session.current if session is not None else None
    if record is None or getattr(result, "_previousTestClass", None) == test.__class__:
        return original(self, test, result)
    # setUpClass reports a skip or an error to the result: Outcomes records it as this test's
    value = _shared(session, record, "setUpClass", "class", original, self, test, Outcomes(result, record))  # type: ignore[arg-type]
    if getattr(test.__class__, "_classSetupFailed", False) and record["outcome"] == "passed":
        record["outcome"] = "error"
    return value


def _shared(session: Session, record: dict[str, Any], name: str, scope: str, fn: Any, *args: Any) -> Any:
    """Set up a class or module fixture in the test's setup phase: its queries and memory are the fixture's."""
    token = session.fixture_started(shared=True)
    try:
        with session.phase(record, "setup") as call:
            return call(fn, *args)
    finally:
        session.fixture_finished(token, name, scope)


def _teardown_previous_class(self: Any, test: Any, result: Any) -> Any:
    """The previous class's teardown: the last test that ran pays for it, as with pytest."""
    original = _original(unittest.TestSuite, "_tearDownPreviousClass")
    if getattr(result, "_previousTestClass", None) in (None, getattr(test, "__class__", None)):
        return original(self, test, result)
    return _in_last_teardown(original, self, test, result)


def _teardown_module(self: Any, result: Any) -> Any:
    original = _original(unittest.TestSuite, "_handleModuleTearDown")
    if _module_fixture:
        return original(self, result)
    return _in_last_teardown(original, self, result)


def _in_last_teardown(fn: Callable[..., Any], *args: Any) -> Any:
    session = Session.active
    if session is None:
        return fn(*args)
    _end_current(session)
    if session.pending is None:
        return fn(*args)
    with session.phase(session.pending[0], "teardown") as call:
        return call(fn, *args)


def _run_test(self: Any, result: Any = None) -> Any:
    return _outermost(self, result, _original(unittest.TestCase, "run"))


def _setup_and_call(self: Any, result: Any, debug: bool = False) -> Any:  # noqa: FBT001, FBT002
    original = _original(django_module("testcases").SimpleTestCase, "_setup_and_call")
    return _outermost(self, result, lambda test, outcomes: original(test, outcomes, debug))


def _outermost(test: Any, result: Any, fn: Callable[..., Any]) -> Any:
    """Run a test once: Django's _setup_and_call and unittest's run both come through here, the outer one owns it."""
    session = Session.active
    if session is None or id(test) in _running or result is None:
        return fn(test, result)
    _running.add(id(test))
    record = _begin(session, test)
    try:
        return fn(test, Outcomes(result, record))
    finally:
        _running.discard(id(test))
        if session.current is record:
            session.end_test(record)


def _phase_method(phase: str, key: tuple[type, str]) -> Callable[..., Any]:
    def timed(self_or_cls: Any, *args: Any, **kwargs: Any) -> Any:
        original = _original(*key)
        session = Session.active
        record = session.current if session is not None else None
        if record is None or record.get("_in_phase"):  # a super() call inside the same phase
            return original(self_or_cls, *args, **kwargs)
        record["_in_phase"] = True
        try:
            with session.phase(record, phase) as call:  # type: ignore[union-attr]
                return call(original, self_or_cls, *args, **kwargs)
        finally:
            del record["_in_phase"]

    timed.__name__ = key[1]
    return timed


def _setup_databases(self: Any, *args: Any, **kwargs: Any) -> Any:
    original = _original(django_module("runner").DiscoverRunner, "setup_databases")
    _runner_settings.update(
        keepdb=bool(getattr(self, "keepdb", False)), parallel=getattr(self, "parallel", 1) or 1, django=True
    )
    session = Session.active
    if session is None:
        return original(self, *args, **kwargs)
    token = session.fixture_started(shared=True)
    try:
        return original(self, *args, **kwargs)
    finally:
        session.fixture_finished(token, "django_db_setup", "session")


def _init_worker(*args: Any, **kwargs: Any) -> None:
    """Django's --parallel worker initializer, then a session for the worker (forked, spawned or forkserver)."""
    runner = django_module("runner")
    runner._init_worker(*args, **kwargs)
    install()
    _patch_django()
    session = Session(
        Path.cwd(),
        runner="django",
        argv=[],
        output=os.environ.get("FLAKYTEST_PROFILE_DIR"),
        backend=os.environ.get("FLAKYTEST_PROFILE_BACKEND", "auto"),
        rate=int(os.environ.get("FLAKYTEST_PROFILE_RATE") or 0) or None,
        worker=f"django-{runner._worker_id}",
    )
    session.start(markers=False)
    # Pool workers skip atexit; multiprocessing's own finalizers run when the pool shuts them down
    util.Finalize(None, _finish_worker, args=(session,), exitpriority=100)


def _finish_worker(session: Session) -> None:
    _end_current(session)
    session.finish("OK", _context())


class Outcomes:
    """The test result, passed on to the test in its place: records the outcome, and hands everything on."""

    __slots__ = ("_record", "_result")

    def __init__(self, result: Any, record: dict[str, Any]) -> None:
        object.__setattr__(self, "_result", result)
        object.__setattr__(self, "_record", record)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._result, name)

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._result, name, value)

    def _failed(self, outcome: str, err: Any) -> None:
        record = self._record
        record["outcome"] = outcome
        if err is not None and not record["longrepr"]:
            try:
                record["longrepr"] = "".join(traceback.format_exception(*err))
            except Exception:
                record["longrepr"] = repr(err)

    def addSuccess(self, test: Any) -> None:  # noqa: N802 (unittest's names)
        self._result.addSuccess(test)

    def addFailure(self, test: Any, err: Any) -> None:  # noqa: N802
        self._failed("failed", err)
        self._result.addFailure(test, err)

    def addError(self, test: Any, err: Any) -> None:  # noqa: N802
        self._failed("failed" if "call" in self._record["phases"] else "error", err)
        self._result.addError(test, err)

    def addSubTest(self, test: Any, subtest: Any, err: Any) -> None:  # noqa: N802
        if err is not None:
            self._failed("failed", err)
        self._result.addSubTest(test, subtest, err)

    def addSkip(self, test: Any, reason: str) -> None:  # noqa: N802
        if self._record["outcome"] == "passed":
            self._record["outcome"] = "skipped"
        skip = getattr(self._result, "addSkip", None)
        if skip is None:
            self._result.addSuccess(test)
        else:
            skip(test, reason)

    def addExpectedFailure(self, test: Any, err: Any) -> None:  # noqa: N802
        self._record["outcome"] = "xfailed"
        self._result.addExpectedFailure(test, err)

    def addUnexpectedSuccess(self, test: Any) -> None:  # noqa: N802
        self._record["outcome"] = "xpassed"
        self._result.addUnexpectedSuccess(test)
