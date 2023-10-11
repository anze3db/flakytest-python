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
import subprocess
import traceback

import urllib3

http = urllib3.PoolManager(timeout=25.0, retries=3)

token = os.environ.get("FLAKYTEST_SECRET_TOKEN")
host = os.environ.get("FLAKYTEST_HOST", "https://flakytest.dev")

muted_tests = []
tests = []


def get_sha():
    if github_sha := os.environ.get("GITHUB_SHA", None):
        return github_sha
    elif gitlab_sha := os.environ.get("CI_COMMIT_SHA", None):
        return gitlab_sha
    elif bitbucket_sha := os.environ.get("BITBUCKET_COMMIT", None):
        return bitbucket_sha
    elif circleci_sha := os.environ.get("CIRCLE_SHA1", None):
        return circleci_sha
    elif travis_sha := os.environ.get("TRAVIS_COMMIT", None):
        return travis_sha

    try:
        process = subprocess.Popen(["git", "rev-parse", "HEAD"], shell=False, stdout=subprocess.PIPE)
        return process.communicate()[0].strip().decode()
    except:
        return None


def get_branch():
    if github_branch := os.environ.get("GITHUB_REF_NAME", None):
        return github_branch
    elif gitlab_branch := os.environ.get("CI_COMMIT_REF_NAME", None):
        return gitlab_branch
    elif bitbucket_branch := os.environ.get("BITBUCKET_BRANCH", None):
        return bitbucket_branch
    elif circleci_branch := os.environ.get("CIRCLE_BRANCH", None):
        return circleci_branch
    elif travis_branch := os.environ.get("TRAVIS_BRANCH", None):
        return travis_branch

    try:
        process = subprocess.Popen(["git", "rev-parse", "--abbrev-ref", "HEAD"], shell=False, stdout=subprocess.PIPE)
        return process.communicate()[0].strip().decode()
    except:
        return None


def get_run_username():
    if github_username := os.environ.get("GITHUB_ACTOR", None):
        return github_username
    elif gitlab_username := os.environ.get("GITLAB_USER_LOGIN", None):
        return gitlab_username
    elif bitbucket_username := os.environ.get("BITBUCKET_USERNAME", None):
        return bitbucket_username
    elif circleci_username := os.environ.get("CIRCLE_USERNAME", None):
        return circleci_username

    try:
        process = subprocess.Popen(["git", "log", "-1", "--pretty=format:%an"], shell=False, stdout=subprocess.PIPE)
        return process.communicate()[0].strip().decode()
    except:
        return None


def get_run_id():
    if github_run_id := os.environ.get("GITHUB_RUN_ID", None):
        return github_run_id
    elif gitlab_run_id := os.environ.get("CI_PIPELINE_ID", None):
        return gitlab_run_id
    elif bitbucket_run_id := os.environ.get("BITBUCKET_BUILD_NUMBER", None):
        return bitbucket_run_id
    elif circleci_run_id := os.environ.get("CIRCLE_WORKFLOW_ID", None):
        return circleci_run_id
    elif travis_run_id := os.environ.get("TRAVIS_BUILD_ID", None):
        return travis_run_id

    return None


def get_run_name():
    if github_run_name := os.environ.get("GITHUB_ACTION", None):
        return github_run_name
    elif gitlab_run_name := os.environ.get("CI_JOB_NAME", None):
        return gitlab_run_name
    elif circleci_run_name := os.environ.get("CIRCLE_JOB", None):
        return circleci_run_name
    elif travis_run_name := os.environ.get("TRAVIS_JOB_NAME", None):
        return travis_run_name

    return None


def get_run_attempt():
    if github_run_attempt := os.environ.get("GITHUB_RUN_ATTEMPT", None):
        return github_run_attempt

    return 1


def get_env_data():
    return {
        "ci": os.environ.get("CI", False),
        "run_id": get_run_id(),
        "run_name": get_run_name(),
        "run_username": get_run_username(),
        "run_attempt": get_run_attempt(),
        "branch": get_branch(),
        "sha": get_sha(),
    }


def pytest_collection_modifyitems(items):
    if not token:
        return

    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    response = http.request("GET", f"{host}/muted_tests/", headers=headers)
    muted_tests[:] = response.json()["result"]
    muted_test_str = "\n  ".join(test["name"] for test in muted_tests)
    if muted_tests:
        print(f"\nFlakytest muted tests:\n  {muted_test_str}\n")
    muted_test_set = {test["name"] for test in muted_tests}
    items[:] = [item for item in items if item.nodeid not in muted_test_set]


def pytest_collection_finish(session):
    if not token:
        return

    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    response = http.request("POST", f"{host}/sessions/", json=get_env_data(), headers=headers)
    response_json = response.json()
    if "message" in response_json:
        print(f"\n{response_json['message']}")
    session.stash["session_id"] = response.json()["session_id"]


def pytest_sessionfinish(session, exitstatus):
    # Send report end here?
    if not token:
        return
    json_data = {"tests": tests + muted_tests, "exit_status": exitstatus.name if exitstatus != 0 else "OK"}
    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    response = http.request(
        "POST", f"{host}/sessions/{session.stash['session_id']}/finish", json=json_data, headers=headers
    )
    print(f"\n{response.json()['message']}")


def pytest_runtest_makereport(item, call):
    if call.when == "call":
        # Get the test status
        if call.excinfo is None:
            status = "PASS"
        elif call.excinfo.type == AssertionError:
            status = "FAIL"
        else:
            status = "ERROR"

        tests.append(
            {
                "name": item.nodeid,
                "status": status,
                "duration": call.duration,
                "output": "\n".join(traceback.format_tb(call.excinfo.tb)) if call.excinfo else "",
            }
        )
