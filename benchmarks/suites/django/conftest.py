"""Records timings for the benchmark harness: wall clock marks and this process's CPU (and its children's)."""

import json
import os
import time

import pytest

MARKS = {"start": time.perf_counter()}


def pytest_sessionstart():
    MARKS["session_start"] = time.perf_counter()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtestloop():
    MARKS["tests_start"] = time.perf_counter()
    yield
    MARKS["tests_end"] = time.perf_counter()


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    out = os.environ.get("BENCH_OUT")
    if not out or hasattr(config, "workerinput"):
        return
    MARKS["end"] = time.perf_counter()
    times = os.times()
    data = {
        "marks": MARKS,
        "self_cpu": times.user + times.system,
        "children_cpu": times.children_user + times.children_system,
    }
    with open(out, "w") as f:
        json.dump(data, f)
