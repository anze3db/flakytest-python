import pytest

pytest_plugins = ["pytester"]


@pytest.fixture(autouse=True)
def _plain_pytest_inside(monkeypatch):
    """The suites tests run inside get flakytest only when they ask for it (PROFILE), however this suite was run:
    `flakytest pytest` puts the plugin in PYTEST_PLUGINS, which subprocesses inherit."""
    monkeypatch.delenv("PYTEST_PLUGINS", raising=False)
