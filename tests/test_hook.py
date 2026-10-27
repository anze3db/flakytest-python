from importlib import metadata
from types import SimpleNamespace

from flakytest import hook
from flakytest.hook import get_installed_packages


def test_get_installed_packages():
    packages = get_installed_packages()

    assert isinstance(packages, dict)
    assert packages
    assert packages["pytest"] == metadata.version("pytest")
    assert packages["flakytest"] == metadata.version("flakytest")
    assert all(name == name.lower() for name in packages)


def test_xdist_workers_dont_report_but_know_muted_tests(monkeypatch):
    requests = []
    monkeypatch.setattr(hook, "token", "secret")
    monkeypatch.setattr(hook, "make_request", lambda *args, **kwargs: requests.append(args))
    monkeypatch.setattr(hook, "stash", {})
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    monkeypatch.setenv(hook.MUTED_TESTS_ENV, '["tests/test_a.py::test_flaky"]')
    # the profiler is already registered by pytest_load_initial_conftests
    config = SimpleNamespace(pluginmanager=SimpleNamespace(has_plugin=lambda name: name == "flakytest-profiler"))
    hook.pytest_configure(config)
    assert requests == []
    assert hook.stash == {"muted_tests": {"tests/test_a.py::test_flaky"}}


def test_only_unsuccessful_tests_send_output(monkeypatch):
    monkeypatch.setattr(hook, "stash", {"ingest_id": 1, "tests": []})

    def report(outcome, **kwargs):
        return SimpleNamespace(
            nodeid=f"tests/test_a.py::test_{outcome}",
            when="call",
            outcome=outcome,
            location=("tests/test_a.py", 1, f"test_{outcome}"),
            keywords={"test_a": 1},
            longrepr="Traceback" if outcome == "failed" else None,
            sections=[("Captured stdout call", "hello")],
            duration=0.1,
            caplog="log",
            capstderr="err",
            capstdout="x" * (hook.MAX_OUTPUT_LENGTH + 10),
            count_towards_summary=True,
            failed=outcome == "failed",
            passed=outcome == "passed",
            skipped=False,
        )

    hook.pytest_report_teststatus(report("passed"))
    hook.pytest_report_teststatus(report("failed"))
    passed, failed = hook.stash["tests"]
    assert (passed["capstdout"], passed["caplog"], passed["keywords"], passed["sections"]) == ("", "", {}, [])
    assert failed["longrepr"] == "Traceback"
    assert failed["caplog"] == "log"
    assert failed["capstdout"].endswith("x" * hook.MAX_OUTPUT_LENGTH)
    assert failed["capstdout"].startswith("[… truncated by flakytest …]")
