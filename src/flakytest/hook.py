# SPDX-FileCopyrightText: 2023-present Anže Pečar <anze@pecar.me>
#
# SPDX-License-Identifier: MIT

# root
# └── pytest_cmdline_main
#  ├── pytest_plugin_registered
#  ├── pytest_configure
#  │ └── pytest_plugin_registered
#  ├── pytest_sessionstart
#  │ ├── pytest_plugin_registered
#  │ └── pytest_report_header
#  ├── pytest_collection
#  │ ├── pytest_collectstart
#  │ ├── pytest_make_collect_report
#  │ │ ├── pytest_collect_file
#  │ │ │ └── pytest_pycollect_makemodule
#  │ │ └── pytest_pycollect_makeitem
#  │ │ └── pytest_generate_tests
#  │ │ └── pytest_make_parametrize_id
#  │ ├── pytest_collectreport
#  │ ├── pytest_itemcollected
#  │ ├── pytest_collection_modifyitems
#  │ └── pytest_collection_finish
#  │ └── pytest_report_collectionfinish
#  ├── pytest_runtestloop
#  │ └── pytest_runtest_protocol
#  │ ├── pytest_runtest_logstart
#  │ ├── pytest_runtest_setup
#  │ │ └── pytest_fixture_setup
#  │ ├── pytest_runtest_makereport
#  │ ├── pytest_runtest_logreport
#  │ │ └── pytest_report_teststatus
#  │ ├── pytest_runtest_call
#  │ │ └── pytest_pyfunc_call
#  │ ├── pytest_runtest_teardown
#  │ │ └── pytest_fixture_post_finalizer
#  │ └── pytest_runtest_logfinish
#  ├── pytest_sessionfinish
#  │ └── pytest_terminal_summary
#  └── pytest_unconfigure

# def pytest_terminal_summary(terminalreporter, exitstatus, config):
#     # Add a section?
#     ...
import os
import sys
from importlib import metadata

import pytest
from _pytest.reports import TestReport  # Needed for 6.x support
from _pytest.runner import CallInfo  # # Needed for 6.x support
from pytest import Item

from flakytest.__about__ import __version__
from flakytest.cloud import logger, make_request, stash, token
from flakytest.env_vars import env_vars
from flakytest.environment import detect_environment, get_git_data
from flakytest.profiling.plugin import ProfilerPlugin, add_options, profiling_requested

test_batch_size = int(os.environ.get("FLAKYTEST_TEST_BATCH_SIZE", "200"))


def get_env_data():
    return {env_var: os.environ.get(env_var) for env_var in env_vars if env_var in os.environ}


def get_installed_packages():
    packages = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if not name:
            continue
        # The same package can be on sys.path more than once, the first one found wins (same as pkg_resources)
        packages.setdefault(name.lower(), dist.metadata.get("Version"))
    return dict(sorted(packages.items()))


def pytest_addoption(parser):
    add_options(parser)


@pytest.hookimpl(tryfirst=True)
def pytest_load_initial_conftests(early_config):
    """Start profiling before conftest.py files (and Django, …) are imported, so startup is measured too."""
    if profiling_requested(early_config) and ProfilerPlugin.active is None:
        plugin = ProfilerPlugin(early_config)
        plugin.start()
        early_config.pluginmanager.register(plugin, "flakytest-profiler")


def pytest_configure(config):
    """First pytest hook called

    Send env/git/python data to host, retrieve ingest id and muted tests and store them in the config.stash"""
    if profiling_requested(config) and not config.pluginmanager.has_plugin("flakytest-profiler"):
        if ProfilerPlugin.active is None:  # e.g. options passed through pytest.main() skip initial conftests
            plugin = ProfilerPlugin(config)
            plugin.start()
            config.pluginmanager.register(plugin, "flakytest-profiler")

    if not token:
        return

    response_json = make_request(
        "/ingest/start",
        {
            "env": get_env_data(),
            "environment": detect_environment(),
            "git": get_git_data(),
            "python_version": sys.version,
            "packages": get_installed_packages(),
            "version": __version__,
        },
    )
    if not response_json:
        return

    stash["ingest_id"] = response_json.get("ingest_id")
    muted_tests = stash["muted_tests"] = {test["name"] for test in response_json.get("muted_tests", [])}
    stash["tests"] = []

    if muted_tests:
        muted_test_str = "\n  ".join(test for test in muted_tests)
        logger.info(f"\nFlakytest muted tests:\n  {muted_test_str}\n")


def pytest_runtest_makereport(item: Item, call: CallInfo[None]) -> TestReport:
    """Second pytest hook, called after each test.

    If test is muted, change the outcome to mute-failed or mute-passed.
    """

    muted_tests = stash.get("muted_tests", {})
    report = TestReport.from_item_and_call(item, call)

    if item.nodeid in muted_tests:
        if call.when == "call":
            # pytest only types passed/failed/skipped, muted tests get custom outcomes that flakytest.dev uses
            if report.failed:
                report.outcome = "mute-failed"  # type: ignore[assignment]
            else:
                report.outcome = "mute-passed"  # type: ignore[assignment]
    return report


def pytest_report_teststatus(report):
    """Third pytest hook, called after each test's report has been created.

    Add test data to the tests list in config.stash and send it to the host if the list is long enough.
    """
    ingest_id = stash.get("ingest_id", None)
    if not ingest_id:
        return

    if report.when == "setup" and report.outcome == "skipped":
        pass
    elif report.when != "call":
        return

    tests = stash.get("tests", [])

    tests.append(
        {
            "name": report.nodeid,
            "location": report.location,
            "keywords": report.keywords,
            "outcome": report.outcome,
            "longrepr": str(report.longrepr) if report.longrepr else None,
            "sections": report.sections,
            "duration": report.duration,
            "caplog": report.caplog,
            "capstderr": report.capstderr,
            "capstdout": report.capstdout,
            "count_towards_summary": report.count_towards_summary,
            "failed": True if report.outcome == "mute-failed" else report.failed,
            "passed": False if report.outcome == "mute-failed" else report.passed,
            "skipped": report.skipped,
            "muted": True if report.outcome in ("mute-failed", "mute-passed") else False,
        }
    )
    if len(tests) >= test_batch_size:
        json_data = {
            "ingest_id": ingest_id,
            "tests": tests,
        }
        make_request("/ingest/progress", json_data)
        tests.clear()

    if report.outcome == "mute-failed":
        return "muted", "M", ("MUTE", {"red": True})
    elif report.outcome == "mute-passed":
        return "muted", "m", ("MUTE", {"green": True})


def pytest_sessionfinish(exitstatus):
    """Last pytest hook, called after all tests are done.

    Send the remaining tests to the host along with the exit status."""
    ingest_id = stash.get("ingest_id", None)
    tests = stash.get("tests", [])
    if not ingest_id:
        return

    json_data = {
        "ingest_id": ingest_id,
        "tests": tests,
        "exit_status": exitstatus.name if exitstatus != 0 else "OK",
    }
    make_request("/ingest/end", json_data)
