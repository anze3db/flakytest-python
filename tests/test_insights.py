from flakytest.profiling.analysis import build_profile
from tests.helpers import phase_stack, process, record, run_info, write_tachyon_samples

SITE = "/venv/lib/python3.15/site-packages"


def build(tmp_path, tests, stacks=(), context=None, **run):
    samples = write_tachyon_samples(tmp_path / "samples-1.json", list(stacks)) if stacks else None
    return build_profile(run_info(**run), [process(tests, samples, sampled_seconds=1.0)], tmp_path, context or {})


def ids(doc):
    return [i["id"] for i in doc["insights"]]


def test_password_hashing_with_django_settings(tmp_path):
    nodeid = "tests/test_auth.py::test_login"
    test_file = str(tmp_path / "tests/test_auth.py")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_auth.py").write_text("\n" * 4 + "    client.login(username='a', password='b')\n")
    stacks = [
        (
            phase_stack(
                nodeid, "call", (test_file, "test_login", 5), (f"{SITE}/django/contrib/auth/hashers.py", "encode", 1)
            ),
            900,
        )
    ]
    context = {"django": {"password_hashers": ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]}}
    doc = build(tmp_path, [record(nodeid, call=1.0)], stacks, context)
    hashing = doc["insights"][0]
    assert hashing["id"] == "password-hashing"
    assert "MD5PasswordHasher" in hashing["fix"]
    assert "force_login" in hashing["fix"]
    assert hashing["locations"][0]["location"] == "tests/test_auth.py:5"
    assert hashing["tests"] == [nodeid]


def test_parallelism_suggested_for_long_serial_suites(tmp_path):
    tests = [record(f"tests/test_{i}.py::test_{i}", call=1.0) for i in range(40)]
    doc = build(tmp_path, tests, backend="none", cpu_count=8)
    xdist = next(i for i in doc["insights"] if i["id"] == "xdist")
    assert xdist["impact"] == 35.0  # 40s serially, ~5s on 8 CPUs


def test_no_parallelism_suggestion_for_short_suites(tmp_path):
    doc = build(tmp_path, [record("tests/test_a.py::test_a", call=1.0)], backend="none")
    assert "xdist" not in ids(doc)


def test_n_plus_one_from_repeated_selects(tmp_path):
    db = {"queries": 40, "time": 0.4, "duplicates": [{"sql": "SELECT * FROM item WHERE order_id = %s", "count": 30}]}
    doc = build(tmp_path, [record("tests/test_a.py::test_a", call=1.0, db=db)], backend="none")
    insight = next(i for i in doc["insights"] if i["id"] == "n-plus-one")
    assert "select_related" in insight["fix"] or "selectinload" in insight["fix"]
    assert insight["evidence"] == ["30x in test_a: SELECT * FROM item WHERE order_id = %s"]


def test_expensive_function_scoped_fixture(tmp_path):
    fixture = {"name": "api_client", "scope": "function", "duration": 0.2}
    tests = [record(f"tests/test_api.py::test_{i}", setup=0.2, call=0.01, fixtures=[fixture]) for i in range(10)]
    doc = build(tmp_path, tests, backend="none")
    insight = next(i for i in doc["insights"] if i["id"] == "fixture-scope-api_client")
    assert len(insight["tests"]) == 10
    assert "scope='module'" in insight["fix"]


def test_insights_are_sorted_by_impact(tmp_path):
    fixture = {"name": "api_client", "scope": "function", "duration": 0.2}
    tests = [record(f"tests/test_{i}.py::test_{i}", setup=0.2, call=1.0, fixtures=[fixture]) for i in range(40)]
    doc = build(tmp_path, tests, backend="none")
    impacts = [i["impact"] for i in doc["insights"]]
    assert impacts == sorted(impacts, reverse=True)
