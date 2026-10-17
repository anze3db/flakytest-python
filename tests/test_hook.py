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
    config = SimpleNamespace(
        known_args_namespace=SimpleNamespace(flakytest_profile=False), getoption=lambda name: False
    )
    hook.pytest_configure(config)
    assert requests == []
    assert hook.stash == {"muted_tests": {"tests/test_a.py::test_flaky"}}
