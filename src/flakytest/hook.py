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
import logging
import os
import subprocess
import sys

import pkg_resources
import urllib3
from _pytest.reports import TestReport  # Needed for 6.x support
from _pytest.runner import CallInfo  # # Needed for 6.x support
from dotenv import load_dotenv
from pytest import Item

from flakytest.__about__ import __version__
from flakytest.env_vars import env_vars

load_dotenv()

logger = logging.getLogger("flakytest")

http = urllib3.PoolManager(timeout=25.0, retries=3)

token = os.environ.get("FLAKYTEST_SECRET_TOKEN")
host = os.environ.get("FLAKYTEST_HOST", "https://flakytest.dev") + "/api/v1"
test_batch_size = int(os.environ.get("FLAKYTEST_TEST_BATCH_SIZE", "200"))
stash = {}  # Can't use Stash from Pytest 7.x because it's not available in 6.x


def run_git_command(command):
    try:
        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return process.communicate()[0].strip().decode()
    except:
        return None


def get_git_data():
    delimiter = "%n-___-___-%n"
    fmt = {
        "commit_hash": "%H",
        "tree_hash": "%T",
        "parent_hash": "%P",
        "author_name": "%an",
        "author_email": "%ae",
        "author_date": "%aI",
        "committer_name": "%cn",
        "committer_email": "%ce",
        "committer_date": "%cI",
        "ref_names": "%D",
        "encoding": "%e",
        "subject": "%s",
        "body": "%b",
        "notes": "%N",
        "signature_status": "%G?",
        "signer_name": "%GS",
        "signer_key": "%GK",
        "signer_fingerprint": "%GF",
        "signer_trustlevel": "%GF",
    }
    log = run_git_command(["git", "log", "-1", f"--pretty=format:{delimiter.join(fmt.values())}"])
    if log:
        log = log.split(delimiter.replace("%n", "\n"))
        log_dict = dict(zip(fmt.keys(), log))
    else:
        log_dict = {}

    branch = run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        log_dict["branch"] = branch

    return log_dict


def get_env_data():
    return {env_var: os.environ.get(env_var) for env_var in env_vars if env_var in os.environ}


def make_request(url, json_data):
    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    try:
        response = http.request("POST", host + url, json=json_data, headers=headers)
    except:
        return None
    if response.status != 200:
        try:
            message = response.json()["message"]
        except:
            message = ""
        logger.error(f"Flakytest: Failed to send data to {host}{url} non 200 response]\n{message}")
        return None
    response_json = response.json()
    if "message" in response_json:
        logger.warning(f"\n{response_json['message']}")
    return response_json


def get_installed_packages():
    installed_packages = pkg_resources.working_set
    installed_packages_list = sorted([f"{i.key}=={i.version}" for i in installed_packages])
    return {package.split("==")[0]: package.split("==")[1] for package in installed_packages_list}


def pytest_configure():
    """First pytest hook called

    Send env/git/python data to host, retrieve ingest id and muted tests and store them in the config.stash"""
    if not token:
        return

    response_json = make_request(
        "/ingest/start",
        {
            "env": get_env_data(),
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
            if report.failed:
                report.outcome = "mute-failed"
            else:
                report.outcome = "mute-passed"
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
