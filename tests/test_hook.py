from importlib import metadata

from flakytest.hook import get_installed_packages


def test_get_installed_packages():
    packages = get_installed_packages()

    assert isinstance(packages, dict)
    assert packages
    assert packages["pytest"] == metadata.version("pytest")
    assert packages["flakytest"] == metadata.version("flakytest")
    assert all(name == name.lower() for name in packages)
