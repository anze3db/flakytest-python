import sys

import pytest

from flakytest.profiling import unittest_support
from flakytest.profiling.samplers import choose_backend
from flakytest.report import runs

SUITE = """
import time
import unittest


def setUpModule():
    time.sleep(0.05)


class TestThings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        time.sleep(0.1)

    def setUp(self):
        time.sleep(0.02)

    def test_sleeps(self):
        time.sleep(0.5)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise ValueError("boom")

    @unittest.skip("not today")
    def test_skipped(self):
        pass

    @unittest.expectedFailure
    def test_xfail(self):
        self.assertTrue(False)

    def test_subtests(self):
        for i in range(3):
            with self.subTest(i=i):
                self.assertLess(i, 2)


class TestSkippedClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise unittest.SkipTest("no service")

    def test_a(self):
        pass


class TestBrokenClass(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("no db")

    def test_b(self):
        pass


class TestAsync(unittest.IsolatedAsyncioTestCase):
    async def test_async(self):
        pass
"""

DJANGO_SETTINGS = """
SECRET_KEY = "test"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes"]
DATABASES = {"default": {"ENGINE": "django.db.backends.sqlite3", "NAME": "db.sqlite3"}}
USE_TZ = True
"""

MANAGE = """
import os
import sys

if __name__ == "__main__":
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)
"""

DJANGO_TESTS = """
import time

from django.contrib.auth.models import Group
from django.test import TestCase


class TestGroups(TestCase):
    @classmethod
    def setUpTestData(cls):
        Group.objects.create(name="shared")

    def setUp(self):
        Group.objects.bulk_create(Group(name=f"g{i}") for i in range(20))

    def test_count(self):
        self.assertEqual(Group.objects.count(), 21)

    def test_names(self):
        self.assertEqual(Group.objects.filter(name="shared").count(), 1)


class TestOther(TestCase):
    def test_sleeps(self):
        time.sleep(0.05)

    def test_fails(self):
        self.assertEqual(Group.objects.count(), 1)
"""


def run_python(pytester, *args):
    return pytester.run(sys.executable, "-m", "flakytest", "python", "--flakytest-backend", "none", *args)


def latest(pytester):
    return runs.load_profile(runs.resolve_run(pytester.path, "latest"))


def django_project(pytester, tests=DJANGO_TESTS):
    pytest.importorskip("django")
    pytester.makepyfile(settings=DJANGO_SETTINGS, manage=MANAGE)
    pytester.mkpydir("app")
    pytester.path.joinpath("app", "tests.py").write_text(tests)


def test_parse():
    assert unittest_support.parse(["-m", "unittest", "-v"]) == ({}, "module", "unittest", ["-v"])
    assert unittest_support.parse(["--flakytest-rate=50", "manage.py", "test"]) == (
        {"--flakytest-rate": "50"},
        "script",
        "manage.py",
        ["test"],
    )
    assert unittest_support.parse(["-munittest"])[1:3] == ("module", "unittest")
    for args in ([], ["-X", "dev", "-m", "unittest"], ["--flakytest-nope", "x"], ["--flakytest-backend", "x", "y"]):
        with pytest.raises(unittest_support.UsageError):
            unittest_support.parse(args)


def test_unittest(pytester):
    pytester.makepyfile(test_things=SUITE)
    result = run_python(pytester, "-m", "unittest")
    assert result.ret == 1  # unittest's exit status, untouched
    result.stderr.fnmatch_lines(
        ["FAILED (failures=2, errors=2, skipped=2, expected failures=1)", "*flakytest profile*"]
    )
    result.stderr.fnmatch_lines(["*9 tests in*", "Report: .flakytest/runs/*/index.html"])

    profile = latest(pytester)
    assert profile["run"]["runner"] == "unittest"
    assert profile["run"]["exit_status"] == "TESTS_FAILED"
    tests = {t["nodeid"]: t for t in profile["tests"]}
    assert {nodeid: t["outcome"] for nodeid, t in tests.items()} == {
        "test_things.py::TestThings::test_sleeps": "passed",
        "test_things.py::TestThings::test_fails": "failed",
        "test_things.py::TestThings::test_errors": "failed",
        "test_things.py::TestThings::test_skipped": "skipped",
        "test_things.py::TestThings::test_xfail": "xfailed",
        "test_things.py::TestThings::test_subtests": "failed",
        "test_things.py::TestSkippedClass::test_a": "skipped",
        "test_things.py::TestBrokenClass::test_b": "error",
        "test_things.py::TestAsync::test_async": "passed",
    }
    sleeps = tests["test_things.py::TestThings::test_sleeps"]
    assert sleeps["class"] == "TestThings"
    assert sleeps["phases"]["call"] >= 0.5
    assert 0.02 <= sleeps["phases"]["setup"] < 0.1  # setUp, but not setUpClass: that's the first test's
    assert "AssertionError: 1 != 2" in tests["test_things.py::TestThings::test_fails"]["longrepr"]
    assert "ValueError: boom" in tests["test_things.py::TestThings::test_errors"]["longrepr"]
    assert "RuntimeError: no db" in tests["test_things.py::TestBrokenClass::test_b"]["longrepr"]
    assert "2 not less than 2" in tests["test_things.py::TestThings::test_subtests"]["longrepr"]
    # setUpModule and setUpClass are shared fixtures, timed in the setup of the first test that needs them
    fixtures = {(f["name"], f["scope"]): f for f in profile["fixtures"]}
    assert fixtures["setUpModule", "module"]["max"] >= 0.05
    assert fixtures["setUpClass", "class"]["max"] >= 0.1
    first = next(t for t in profile["tests"] if t["class"] == "TestThings")
    assert first["phases"]["setup"] >= 0.1


def test_unittest_script_and_code(pytester):
    pytester.makepyfile(test_things="import unittest\n\nclass TestA(unittest.TestCase):\n    def test_a(self): pass\n")
    pytester.makepyfile(run_tests="import unittest\nunittest.main(module='test_things')\n")
    assert run_python(pytester, "run_tests.py").ret == 0
    assert [t["nodeid"] for t in latest(pytester)["tests"]] == ["test_things.py::TestA::test_a"]
    assert run_python(pytester, "-c", "import unittest; unittest.main(module='test_things', exit=False)").ret == 0
    assert latest(pytester)["summary"]["passed"] == 1


def test_python_m_pytest_runs_the_pytest_plugin(pytester):
    pytester.makepyfile(test_suite="def test_a():\n    pass\n")
    result = run_python(pytester, "-m", "pytest", "-p", "no:cacheprovider")
    assert result.ret == 0
    assert latest(pytester)["run"]["runner"] == "pytest"


@pytest.mark.skipif(choose_backend("auto")[0] == "none", reason="no sampling profiler available")
def test_unittest_sampled(pytester):
    pytester.makepyfile(test_things=SUITE)
    pytester.run(sys.executable, "-m", "flakytest", "python", "--flakytest-rate", "500", "-m", "unittest")
    sleeps = next(t for t in latest(pytester)["tests"] if t["name"] == "test_sleeps")
    assert sleeps["samples"]["call"] >= 10
    assert sleeps["categories"].get("sleep", 0) >= 0.4
    assert any(segment["label"] == "test body" for segment in sleeps["segments"]), sleeps["segments"]
    assert any(segment["label"] == "setUp" for segment in sleeps["segments"]), sleeps["segments"]


def check_django_run(profile, fails="failed"):
    assert profile["run"]["runner"] == "django"
    tests = {t["nodeid"]: t for t in profile["tests"]}
    assert {nodeid: t["outcome"] for nodeid, t in tests.items()} == {
        "app/tests.py::TestGroups::test_count": "passed",
        "app/tests.py::TestGroups::test_names": "passed",
        "app/tests.py::TestOther::test_sleeps": "passed",
        "app/tests.py::TestOther::test_fails": fails,
    }
    assert tests["app/tests.py::TestOther::test_sleeps"]["phases"]["call"] >= 0.05
    # setUp's queries are the test's own; setUpTestData's are its class fixture's
    count = tests["app/tests.py::TestGroups::test_count"]
    assert count["db"]["queries"] >= 2
    if fails == "failed":
        assert "AssertionError" in tests["app/tests.py::TestOther::test_fails"]["longrepr"]
    fixtures = {f["name"]: f for f in profile["fixtures"]}
    assert fixtures["django_db_setup"]["scope"] == "session"
    assert fixtures["setUpClass"]["calls"] == 2
    assert not [w for w in profile["warnings"] if w["code"] == "missing_workers"], profile["warnings"]
    return tests


def test_django(pytester):
    django_project(pytester)
    result = run_python(pytester, "manage.py", "test", "app")
    assert result.ret == 1
    result.stderr.fnmatch_lines(["*flakytest profile*", "4 tests in*"])
    profile = latest(pytester)
    tests = check_django_run(profile)
    assert {t["worker"] for t in tests.values()} == {None}


def test_django_parallel(pytester):
    # A failure in a worker needs tblib to reach the parent, so it skips instead
    django_project(pytester, DJANGO_TESTS.replace("self.assertEqual(Group.objects.count(), 1)", "self.skipTest('-')"))
    result = run_python(pytester, "manage.py", "test", "app", "--parallel", "2")
    assert result.ret == 0
    profile = latest(pytester)
    assert profile["run"]["workers"] == 2
    assert profile["run"]["postprocessing"]["tests_finished_at"] > 0
    tests = check_django_run(profile, fails="skipped")
    # Django hands each worker whole classes
    workers = {cls: {t["worker"] for t in tests.values() if t["class"] == cls} for cls in ("TestGroups", "TestOther")}
    assert all(len(names) == 1 for names in workers.values()), workers
    assert {name for names in workers.values() for name in names} <= {"django-1", "django-2"}


INHERITED = """
import unittest


class Checks:
    def test_shared(self):
        pass


class TestA(Checks, unittest.TestCase):
    def test_own(self):
        pass


class TestB(TestA):
    pass
"""


def test_unittest_records_inherited_tests(pytester):
    pytester.makepyfile(test_things=INHERITED)
    run_python(pytester, "-m", "unittest")
    tests = {t["nodeid"]: t.get("inherited_from") for t in latest(pytester)["tests"]}
    assert tests == {
        "test_things.py::TestA::test_own": None,
        "test_things.py::TestA::test_shared": "test_things.Checks.test_shared",
        "test_things.py::TestB::test_own": "test_things.TestA.test_own",
        "test_things.py::TestB::test_shared": "test_things.Checks.test_shared",
    }
