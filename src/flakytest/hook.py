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
import http.client
import json
import os
import traceback

token = os.environ.get("FLAKYTEST_SECRET_TOKEN")
host = os.environ.get("FLAKYTEST_HOST", "flakytest.dev")

muted_tests = []
tests = []


def pytest_collection_modifyitems(items):
    if not token:
        return

    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    conn = http.client.HTTPConnection("localhost", 8001)
    conn.request("GET", "/muted_tests/", headers=headers)
    muted_tests[:] = json.loads(conn.getresponse().read().decode())["result"]
    muted_test_set = {test["name"] for test in muted_tests}
    items[:] = [item for item in items if item.nodeid not in muted_test_set]


def pytest_collection_finish(session):
    if not token:
        return

    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    conn = http.client.HTTPConnection(host)
    conn.request("POST", "/sessions/", headers=headers)
    session.stash["session_id"] = conn.getresponse().read().decode()


def pytest_sessionfinish(session, exitstatus):
    # Send report end here?
    if not token:
        return
    json_data = json.dumps(
        {"tests": tests + muted_tests, "exit_status": exitstatus.name if exitstatus != 0 else "OK"}
    ).encode()
    headers = {"Content-type": "application/json", "Accept": "text/plain", "Authorization": token}
    conn = http.client.HTTPConnection(host)
    conn.request("POST", f"/sessions/{session.stash['session_id']}/finish", json_data, headers)


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
