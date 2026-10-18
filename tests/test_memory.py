import sys

import pytest

from flakytest.profiling import memory
from flakytest.profiling.memory import MB, MemoryMeter, growth_warning, summarize
from flakytest.report.text import markdown_report, memory_line
from tests.test_plugin import latest_profile

LINUX = sys.platform.startswith("linux") and memory.STATUS.exists()


def measured(nodeid, worker, at, *, start, peak, end, duration=1.0, shared=10 * MB):
    return {
        "nodeid": nodeid,
        "worker": worker,
        "duration": duration,
        "memory": {
            "at": at,
            "start": start + shared,
            "end": end + shared,
            "peak": peak + shared,
            "peak_increase": peak - start,
            "growth": end - start,
            "private_start": start,
            "private_end": end,
        },
    }


def touch(size):
    data = bytearray(size)
    data[::4096] = b"x" * len(data[::4096])
    return data


def test_summarize_adds_up_workers():
    tests = [
        measured("t.py::a", "gw0", 0.0, start=100 * MB, peak=400 * MB, end=100 * MB),
        measured("t.py::b", "gw1", 0.5, start=100 * MB, peak=150 * MB, end=150 * MB),
        measured("t.py::c", "gw0", 2.0, start=100 * MB, peak=120 * MB, end=110 * MB),
    ]
    summary = summarize(tests, "linux")
    assert summary["peak_per_test"] and summary["private"]
    assert summary["peak"] == 410 * MB  # RSS, with the shared part
    # a's private peak (400 MB) and b's (150 MB) overlap from 0.5s to 1s
    assert summary["machine_peak"] == 550 * MB
    assert summary["machine_peak_at"] == 0.5
    assert summary["machine_peak_tests"] == ["t.py::a", "t.py::b"]
    workers = {p["worker"]: p for p in summary["processes"]}
    assert (workers["gw0"]["tests"], workers["gw0"]["growth"]) == (2, 10 * MB)
    assert workers["gw1"]["growth"] == 50 * MB
    assert max(value for _, value in summary["timeline"]) == 550 * MB


def test_summarize_without_measurements():
    assert summarize([{"nodeid": "t.py::a", "worker": None, "duration": 1.0, "memory": None}], None) is None


def test_growth_warning_names_the_tests_that_grew():
    tests = [measured(f"t.py::leak[{i}]", None, i, start=100 * MB + i * 60 * MB, peak=0, end=0) for i in range(6)]
    for i, test in enumerate(tests):
        test["memory"].update(
            private_end=100 * MB + (i + 1) * 60 * MB, growth=60 * MB + i, peak_increase=60 * MB, peak=None
        )
    warning = growth_warning(summarize(tests, "linux"), tests)
    assert warning["code"] == "memory_growth"
    assert "grew by 360 MB over 6 tests" in warning["message"]
    assert warning["tests"][0] == "t.py::leak[5]"
    small = [measured("t.py::a", None, 0, start=100 * MB, peak=110 * MB, end=110 * MB)]
    assert growth_warning(summarize(small, "linux"), small) is None


@pytest.mark.skipif(not LINUX, reason="resets the peak through /proc")
def test_meter_measures_each_test_on_its_own():
    meter = MemoryMeter()
    assert meter.source == "linux"
    first = meter.start()
    data = touch(80 * MB)
    del data
    big = meter.stop(first)
    second = meter.start()
    data = touch(20 * MB)
    del data
    small = meter.stop(second)
    assert big["peak_increase"] >= 70 * MB
    assert 15 * MB <= small["peak_increase"] < 40 * MB  # not the earlier 80 MB: the peak was reset
    assert big["private_start"] > 0 and "at" in big


def test_meter_without_a_resettable_peak(monkeypatch):
    meter = MemoryMeter()
    meter.source = "maxrss"
    readings = iter([{"peak": 100 * MB}, {"peak": 300 * MB}, {"peak": 300 * MB}, {"peak": 300 * MB}])
    monkeypatch.setattr(meter, "_read", lambda: next(readings))
    raised = meter.stop(meter.start())
    unchanged = meter.stop(meter.start())
    assert (raised["peak"], raised["peak_increase"]) == (300 * MB, 200 * MB)
    assert "peak" not in unchanged and "peak_increase" not in unchanged  # below the process's earlier peak
    assert not meter.peak_per_test


SUITE = """
LEAK = []

def test_big():
    data = bytearray(64 * 1024 * 1024)
    data[::4096] = b"x" * len(data[::4096])

def test_small():
    LEAK.append(bytearray(1024))
"""


@pytest.mark.skipif(not LINUX, reason="exact per-test peaks need Linux")
def test_profile_records_memory(pytester):
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest("--flakytest-profile", "--flakytest-backend", "none")
    result.assert_outcomes(passed=2)
    result.stdout.fnmatch_lines(["Memory: peak * · most: test_suite.py::test_big needed * MB"])
    _, profile = latest_profile(pytester)
    tests = {t["nodeid"]: t for t in profile["tests"]}
    assert tests["test_suite.py::test_big"]["memory"]["peak_increase"] >= 60 * MB
    assert tests["test_suite.py::test_small"]["memory"]["peak_increase"] < 10 * MB
    assert profile["summary"]["memory"]["source"] == "linux"
    assert memory_line(profile).startswith("Memory: peak")
    report = markdown_report(profile)
    assert "## Memory" in report
    assert "| `test_suite.py::test_big` |" in report
