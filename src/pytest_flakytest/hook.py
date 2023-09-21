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

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    # Add a section?
    ...

def pytest_collection_finish(session):
    # Send report start here?
    ...

def pytest_sessionfinish(session, exitstatus):
    # Send report end here?
    ...
