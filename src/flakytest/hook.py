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
import traceback

import urllib3

http = urllib3.PoolManager(timeout=25.0, retries=3)

token = os.environ.get("FLAKYTEST_SECRET_TOKEN")
host = os.environ.get("FLAKYTEST_HOST", "flakytest.dev")

muted_tests = []
tests = []


def get_env_data():
    return {
        "ci": os.environ.get("CI", False),
        "run_id": os.environ.get("GITHUB_RUN_ID", None),
        "run_name": os.environ.get("GITHUB_ACTION", None),
        "run_username": os.environ.get("GITHUB_ACTOR", None),
        "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT", 1),
        "branch": os.environ.get("GITHUB_REF_NAME", None),
        "sha": os.environ.get("GITHUB_SHA", None),
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
