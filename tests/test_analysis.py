import json

import pytest

from flakytest.profiling.analysis import build_profile, rerun_command
from tests.helpers import phase_stack, process, record, run_info, write_tachyon_samples

SLOW = "tests/test_orders.py::TestOrders::test_checkout"
FAST = "tests/test_orders.py::test_fast"


@pytest.fixture
def profile(tmp_path):
    site = "/venv/lib/python3.15/site-packages"
    test_file = str(tmp_path / "tests/test_orders.py")
    fixture_func = (f"{site}/_pytest/fixtures.py", "call_fixture_func", 900)
    checkout = (test_file, "TestOrders.test_checkout", 20)
    stacks = [
        (
            phase_stack(
                SLOW,
                "setup",
                fixture_func,
                (test_file, "user", 8),
                (f"{site}/django/contrib/auth/hashers.py", "encode", 1),
            ),
            300,
        ),
        (phase_stack(SLOW, "call", checkout, (f"{site}/django/db/models/query.py", "get", 10)), 600),
        (phase_stack(SLOW, "call", checkout), 100),
        ((("/venv/bin/pytest", "<module>", 8), ("/x/_pytest/main.py", "perform_collect", 1)), 50),
    ]
    samples = write_tachyon_samples(tmp_path / "samples-1.json", stacks)
    tests = [
        record(
            SLOW, setup=0.3, call=0.7, teardown=0.001, fixtures=[{"name": "user", "scope": "function", "duration": 0.3}]
        ),
        record(FAST, call=0.02),
    ]
    doc = build_profile(run_info(), [process(tests, samples, sampled_seconds=1.05)], tmp_path, {})
    json.dumps(doc)  # the document must be plain JSON
    return doc


def test_samples_are_attributed_to_test_and_phase(profile):
    slow = next(t for t in profile["tests"] if t["nodeid"] == SLOW)
    assert slow["samples"] == {"setup": 300, "call": 700, "teardown": 0}
    assert slow["categories"] == pytest.approx(
        {"password_hashing": 0.3, "db": 0.6, "test_code": 0.1, "unsampled": 0.001}
    )
    # categories always add up to the measured duration
    assert sum(slow["categories"].values()) == pytest.approx(slow["duration"])


def test_segments_and_hot_lines(profile):
    slow = next(t for t in profile["tests"] if t["nodeid"] == SLOW)
    labels = {(s["phase"], s["label"]): s["seconds"] for s in slow["segments"]}
    assert labels == pytest.approx({("call", "test body"): 0.7, ("setup", "fixture user"): 0.3})
    hot = {tuple(profile["frames"][h["frame"]][:3]): h["seconds"] for h in slow["hot_lines"]}
    assert hot == pytest.approx(
        {("tests/test_orders.py", "TestOrders.test_checkout", 20): 0.7, ("tests/test_orders.py", "user", 8): 0.3}
    )


def test_stack_trie_reconstructs_stacks(profile):
    slow = next(t for t in profile["tests"] if t["nodeid"] == SLOW)
    node = slow["stacks"]["call"][0][0]
    frames = []
    while node >= 0:
        parent, frame = profile["stacks"][node]
        frames.append(profile["frames"][frame][1])
        node = parent
    assert frames[::-1] == ["CallInfo.from_call", "TestOrders.test_checkout", "get"]


def test_summary_timeline_and_warnings(profile):
    summary = profile["summary"]
    assert summary["tests"] == 2
    assert summary["timeline"]["collection"] == pytest.approx(0.05)
    assert summary["timeline"]["tests"] == pytest.approx(1.0)
    # A 20ms test with no samples at 1000 Hz should have been sampled: warn and recommend a rate
    warning = next(w for w in profile["warnings"] if w["code"] == "undersampled")
    assert warning["tests"] == [FAST]
    assert warning["recommended_rate"] >= 2000


def test_profile_without_profiler(tmp_path):
    doc = build_profile(run_info(backend="none"), [process([record(FAST, call=0.5)])], tmp_path, {})
    assert doc["tests"][0]["categories"] == {"unsampled": 0.5}
    assert [w["code"] for w in doc["warnings"]] == ["profiler_unavailable"]


def test_shared_fixture_teardown_is_labelled(tmp_path):
    nodeid = "tests/test_api.py::test_last"
    site = "/venv/lib/python3.15/site-packages"
    teardown = (f"{site}/_pytest/fixtures.py", "_teardown_yield_fixture", 1)
    server = (str(tmp_path / "tests/conftest.py"), "live_server", 30)
    samples = write_tachyon_samples(tmp_path / "s.json", [(phase_stack(nodeid, "teardown", teardown, server), 100)])
    fixtures = [{"name": "live_server", "scope": "session", "duration": 0.2}]
    test = record(nodeid, setup=0.2, call=0.01, teardown=0.5, fixtures=fixtures)
    doc = build_profile(run_info(), [process([test], samples, sampled_seconds=0.7)], tmp_path, {})
    result = doc["tests"][0]
    assert result["segments"][0]["label"] == "fixture live_server (session scope)"
    assert result["shared_fixture_time"] == pytest.approx(0.7)


def test_sample_in_the_marker_frame_itself(tmp_path):
    nodeid = "tests/test_a.py::test_a"
    marker_only = phase_stack(nodeid, "call")[:-1]  # without pytest's from_call below the marker
    samples = write_tachyon_samples(tmp_path / "s.json", [(marker_only, 3), (phase_stack(nodeid, "call"), 7)])
    doc = build_profile(run_info(), [process([record(nodeid, call=0.01)], samples, sampled_seconds=0.01)], tmp_path, {})
    test = doc["tests"][0]
    assert test["samples"]["call"] == 10
    assert all(node >= 0 for node, _ in test["stacks"]["call"])


def test_rerun_command():
    nodeids = ["shop/tests/test_orders.py::TestCheckout::test_list", "tests.py::TestA::test_a"]
    assert rerun_command("pytest", 5000, nodeids[:1]) == (
        "flakytest pytest --flakytest-rate 5000 shop/tests/test_orders.py::TestCheckout::test_list"
    )
    assert rerun_command("django", 5000, nodeids) == (
        "flakytest python --flakytest-rate 5000 manage.py test shop.tests.test_orders.TestCheckout.test_list "
        "tests.TestA.test_a"
    )
    assert (
        rerun_command("unittest", 200, nodeids[1:])
        == "flakytest python --flakytest-rate 200 -m unittest tests.TestA.test_a"
    )
