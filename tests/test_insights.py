import sysconfig
from pathlib import Path

import pytest

from flakytest.profiling.analysis import build_profile
from tests.helpers import phase_stack, process, record, run_info, write_tachyon_samples

SITE = "/venv/lib/python3.15/site-packages"
STDLIB = sysconfig.get_paths()["stdlib"]


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


@pytest.mark.parametrize(
    ("runner", "command"), [("django", "manage.py test --parallel auto"), ("unittest", "flakytest pytest -n auto")]
)
def test_parallelism_suggestion_for_other_runners(tmp_path, runner, command):
    tests = [record(f"tests/test_{i}.py::test_{i}", call=1.0) for i in range(40)]
    doc = build(tmp_path, tests, context={"runner": runner}, backend="none", cpu_count=8)
    assert command in next(i for i in doc["insights"] if i["id"] == "xdist")["fix"]


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


def migrations_doc(tmp_path, workers):
    stack = phase_stack("tests/test_a.py::test_a", "setup", (f"{SITE}/django/db/migrations/executor.py", "migrate", 1))
    context = {"pytest": {"pytest_django": "4.11"}}
    return build(
        tmp_path, [record("tests/test_a.py::test_a", setup=4.0, call=0.1)], [(stack, 4000)], context, workers=workers
    )


def test_reuse_test_database(tmp_path):
    insight = next(i for i in migrations_doc(tmp_path, 1)["insights"] if i["id"] == "reuse-test-database")
    assert "per run" in insight["summary"]
    assert "--reuse-db" in insight["fix"]
    assert insight["docs"].endswith("#reuse-db-reuse-the-testing-database-between-test-runs")


def test_reuse_test_database_with_xdist_workers(tmp_path):
    insight = next(i for i in migrations_doc(tmp_path, 4)["insights"] if i["id"] == "reuse-test-database")
    assert "across 4 pytest-xdist workers" in insight["summary"]
    assert "per run" not in insight["summary"]


def test_reuse_test_database_with_django_parallel_workers(tmp_path):
    """Django's workers clone the database the parent migrated: migrations ran once."""
    stack = phase_stack("tests/test_a.py::test_a", "setup", (f"{SITE}/django/db/migrations/executor.py", "migrate", 1))
    context = {"runner": "django", "django": {"databases": ["django.db.backends.sqlite3"]}}
    tests = [record("tests/test_a.py::test_a", setup=4.0, call=0.1)]
    doc = build(tmp_path, tests, [(stack, 4000)], context, workers=4)
    insight = next(i for i in doc["insights"] if i["id"] == "reuse-test-database")
    assert "per run" in insight["summary"]
    assert "manage.py test --keepdb" in insight["fix"]


DB_UTILS = f"{SITE}/django/db/backends/utils.py"
POSTGRES = {"databases": ["django.db.backends.postgresql"]}


def debug_cursor_doc(tmp_path, debug):
    nodeid = "tests/test_a.py::test_queries"
    test = (str(tmp_path / "tests/test_a.py"), "test_queries", 3)
    execute = (DB_UTILS, "CursorDebugWrapper.execute", 120)
    stacks = [
        # the query itself, under the debug cursor: not DEBUG's cost
        (
            phase_stack(
                nodeid, "call", test, execute, (DB_UTILS, "_execute", 100), (f"{SITE}/psycopg/cursor.py", "execute", 9)
            ),
            900,
        ),
        # what DEBUG adds: timing and recording the query
        (phase_stack(nodeid, "call", test, execute, (DB_UTILS, "CursorDebugWrapper.debug_sql", 140)), 100),
    ]
    context = {"django": POSTGRES | {"debug": debug}}
    return build(tmp_path, [record(nodeid, call=10.0)], stacks, context)


def test_debug_cursor_costs_only_its_bookkeeping(tmp_path):
    insight = next(i for i in debug_cursor_doc(tmp_path, debug=True)["insights"] if i["id"] == "django-debug-in-tests")
    assert insight["impact"] == pytest.approx(1.0)  # the debug_sql samples, not 20% of all query time


def test_debug_cursor_without_debug_is_assert_num_queries(tmp_path):
    assert "django-debug-in-tests" not in ids(debug_cursor_doc(tmp_path, debug=False))


def test_wrapping_middleware_owns_only_its_own_work(tmp_path):
    nodeid = "tests/test_api.py::test_schema"
    test = (str(tmp_path / "tests/test_api.py"), "test_schema", 3)
    middleware = (f"{SITE}/whitenoise/middleware.py", "WhiteNoiseMiddleware.__call__", 123)
    view = (str(tmp_path / "app/views.py"), "schema", 10)
    stat = (str(Path(STDLIB) / "genericpath.py"), "isfile", 30)
    stacks = [
        (
            phase_stack(
                nodeid, "call", test, middleware, view, (f"{SITE}/drf_spectacular/generators.py", "get_schema", 1)
            ),
            9000,
        ),
        (phase_stack(nodeid, "call", test, middleware, (f"{SITE}/whitenoise/base.py", "find_file", 5), stat), 10),
    ]
    doc = build(tmp_path, [record(nodeid, call=9.01)], stacks, {"django": {"debug": False}})
    assert "django-static-storage" not in ids(doc)
    big = build(tmp_path, [record(nodeid, call=9.01)], [(stacks[1][0], 9000)], {"django": {"debug": False}})
    static = next(i for i in big["insights"] if i["id"] == "django-static-storage")
    assert static["impact"] == pytest.approx(9.01)  # whitenoise's own lookups, stat() included


def durability_doc(tmp_path, db):
    tests = [record(f"tests/test_{i}.py::test_{i}", call=1.0, db=db) for i in range(40)]
    return build(tmp_path, tests, backend="none", context={"django": POSTGRES})


def test_no_durability_insight_when_nothing_commits(tmp_path):
    db = {
        "queries": 25,
        "time": 0.8,
        "duplicates": [],
        "commits": 0,
        "commit_time": 0.0,
        "autocommit_writes": 0,
        "autocommit_write_time": 0.0,
    }
    assert "db-server-durability" not in ids(durability_doc(tmp_path, db))


def test_durability_insight_from_commits(tmp_path):
    db = {
        "queries": 25,
        "time": 0.8,
        "duplicates": [],
        "commits": 30,
        "commit_time": 0.1,
        "autocommit_writes": 20,
        "autocommit_write_time": 0.05,
    }
    insight = next(i for i in durability_doc(tmp_path, db)["insights"] if i["id"] == "db-server-durability")
    assert insight["impact"] == pytest.approx(6.0)
    assert "1200 commits" in insight["summary"]
    assert "800 writes outside a transaction" in insight["summary"]
