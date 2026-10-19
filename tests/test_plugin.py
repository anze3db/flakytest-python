import json

import pytest

from flakytest.profiling.samplers import choose_backend

SUITE = """
import time
import pytest

@pytest.fixture
def slow_fixture():
    time.sleep(0.05)
    yield 1

class TestThings:
    def test_sleeps(self, slow_fixture):
        time.sleep(0.2)

    def test_fails(self):
        assert False

@pytest.mark.skip
def test_skipped():
    pass

@pytest.mark.xfail
def test_xfail():
    assert False
"""


def latest_profile(pytester):
    runs = pytester.path / ".flakytest" / "runs"
    run = runs / (runs / "latest").read_text().strip()
    return run, json.loads((run / "profile.json").read_text())


def test_profile_without_sampler(pytester):
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest("--flakytest-profile", "--flakytest-backend", "none")
    result.assert_outcomes(passed=1, failed=1, skipped=1, xfailed=1)
    result.stdout.fnmatch_lines(["*flakytest profile*", "*4 tests in*", "Report: *index.html"])

    run, profile = latest_profile(pytester)
    assert (run / "index.html").exists()
    assert (pytester.path / ".flakytest" / ".gitignore").read_text().endswith("*\n")
    tests = {t["nodeid"]: t for t in profile["tests"]}
    assert {nodeid: t["outcome"] for nodeid, t in tests.items()} == {
        "test_suite.py::TestThings::test_sleeps": "passed",
        "test_suite.py::TestThings::test_fails": "failed",
        "test_suite.py::test_skipped": "skipped",
        "test_suite.py::test_xfail": "xfailed",
    }
    sleeps = tests["test_suite.py::TestThings::test_sleeps"]
    assert sleeps["class"] == "TestThings"
    assert sleeps["phases"]["call"] >= 0.2
    slow_fixture = next(f for f in sleeps["fixtures"] if f["name"] == "slow_fixture")
    assert slow_fixture["duration"] >= 0.05
    assert "assert False" in tests["test_suite.py::TestThings::test_fails"]["longrepr"]
    assert profile["run"]["profiler"]["backend"] == "none"
    assert profile["run"]["exit_status"] == "TESTS_FAILED"
    # samples are cleaned up, only the report and profile remain
    assert sorted(p.name for p in run.iterdir() if not p.name.startswith("sampler")) == [
        "index.html",
        "profile.js",
        "profile.json",
        "summary.json",
    ]


def test_not_profiling_by_default(pytester):
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1, failed=1, skipped=1, xfailed=1)
    assert "flakytest profile" not in result.stdout.str()
    assert not (pytester.path / ".flakytest").exists()


@pytest.mark.skipif(choose_backend("auto")[0] == "none", reason="no sampling profiler available")
def test_profile_with_sampler(pytester):
    pytester.makepyfile(test_suite=SUITE)
    result = pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-rate", "500")
    result.assert_outcomes(passed=1, failed=1, skipped=1, xfailed=1)
    _, profile = latest_profile(pytester)
    assert profile["run"]["profiler"]["samples"] > 50
    sleeps = next(t for t in profile["tests"] if t["name"] == "test_sleeps")
    assert sleeps["samples"]["call"] > 50
    assert sleeps["categories"].get("sleep", 0) >= 0.15
    assert any(segment["label"] == "fixture slow_fixture" for segment in sleeps["segments"]), sleeps["segments"]
    assert any(i["id"] == "sleep" for i in profile["insights"])
