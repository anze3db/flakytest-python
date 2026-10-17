import json

import pytest

from flakytest import cloud
from flakytest.profiling.analysis import build_profile
from flakytest.profiling.parts import SESSION_KEYS, size, split_profile
from flakytest.report.tree import build_tree, render_tree
from tests.helpers import phase_stack, process, record, run_info, write_tachyon_samples

SITE = "/venv/lib/python3.15/site-packages"


@pytest.fixture
def profile(tmp_path):
    test_file = str(tmp_path / "tests/test_suite.py")
    nodeids = [f"tests/test_suite.py::test_{i}" for i in range(7)]
    stacks = []
    for i, nodeid in enumerate(nodeids):
        work = (test_file, f"test_{i}", 10 + i)
        stacks.append((phase_stack(nodeid, "call", work, (f"{SITE}/django/db/models/query.py", "get", 5 + i)), 20 + i))
        stacks.append((phase_stack(nodeid, "call", work, (f"{SITE}/django/contrib/auth/hashers.py", "encode", 1)), 30))
    samples = write_tachyon_samples(tmp_path / "samples-1.json", stacks)
    tests = [record(nodeid, call=0.5) for nodeid in nodeids]
    return build_profile(run_info(), [process(tests, samples, sampled_seconds=3.0)], tmp_path, {})


def frames_of(document, frame):
    return document["frames"][frame] if frame is not None else None


def test_small_profiles_are_one_part(profile):
    (part,) = split_profile(profile)
    assert part == profile | {"part": {"index": 0, "count": 1}}


def test_parts_keep_every_test_and_render_the_same(profile):
    parts = split_profile(profile, max_tests=3)
    assert [part["part"] for part in parts] == [{"index": i, "count": 3} for i in range(3)]
    assert [len(part["tests"]) for part in parts] == [3, 3, 1]
    assert sorted(t["nodeid"] for part in parts for t in part["tests"]) == sorted(t["nodeid"] for t in profile["tests"])
    whole = {t["nodeid"]: t for t in profile["tests"]}
    for part in parts:
        assert len(part["frames"]) < len(profile["frames"])
        assert part["run"] == profile["run"] and part["summary"] == profile["summary"]
        for test in part["tests"]:
            original = whole[test["nodeid"]]
            assert render_tree(part, build_tree(part, [test])) == render_tree(profile, build_tree(profile, [original]))
            for new, old in zip(
                test["hot_lines"] + test["segments"], original["hot_lines"] + original["segments"], strict=True
            ):
                assert frames_of(part, new["frame"]) == frames_of(profile, old["frame"])
                assert part["sources"].get(str(new["frame"])) == profile["sources"].get(str(old["frame"]))
            for pairs in test["stacks"].values():
                for node, _ in pairs:
                    assert part["stack_categories"][str(node)]
    json.dumps(parts)


def test_only_the_first_part_has_session_lists(profile):
    first, *rest = split_profile(profile, max_tests=2)
    assert first["insights"]
    for insight, original in zip(first["insights"], profile["insights"], strict=True):
        for new, old in zip(insight["locations"], original["locations"], strict=True):
            assert frames_of(first, new["frame"]) == frames_of(profile, old["frame"])
    for part in rest:
        assert not set(SESSION_KEYS) & part.keys()


def test_parts_are_halved_until_they_fit(profile):
    limit = size(profile) // 3
    parts = split_profile(profile, max_bytes=limit)
    assert len(parts) > 1
    assert all(size(part) <= limit or len(part["tests"]) == 1 for part in parts)


def test_upload_sends_every_part(profile, monkeypatch):
    sent = []

    def make_request(url, data, *, compress=False):
        sent.append((url, data["part"], data.get("ingest_id"), compress))
        return {"url": "https://flakytest.dev/projects/1/profiles/9"}

    monkeypatch.setattr(cloud, "token", "secret")
    monkeypatch.setattr(cloud, "make_request", make_request)
    monkeypatch.setitem(cloud.stash, "ingest_id", 42)
    monkeypatch.setattr(cloud, "split_profile", lambda p: split_profile(p, max_tests=4))
    assert cloud.upload_profile(profile) == "https://flakytest.dev/projects/1/profiles/9"
    assert sent == [
        ("/profile", {"index": 0, "count": 2}, 42, True),
        ("/profile", {"index": 1, "count": 2}, 42, True),
    ]


def test_upload_reports_missing_parts(profile, monkeypatch):
    responses = iter([{"url": "u"}, None])
    monkeypatch.setattr(cloud, "token", "secret")
    monkeypatch.setattr(cloud, "make_request", lambda *args, **kwargs: next(responses))
    monkeypatch.setattr(cloud, "split_profile", lambda p: split_profile(p, max_tests=4))
    assert cloud.upload_profile(profile) is None
