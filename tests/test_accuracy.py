"""Small synthetic suites that reproduce what a profile got wrong on a large Django suite, run for real."""

import json
import os
import sys

import pytest

from flakytest.profiling.memory import MB
from flakytest.profiling.samplers import choose_backend, tachyon_available
from flakytest.report.text import markdown_report, memory_hogs
from tests.test_plugin import latest_profile

SAMPLER = choose_backend("auto")[0] != "none"
LINUX = sys.platform.startswith("linux") and os.path.exists("/proc/self/status")
needs_sampler = pytest.mark.skipif(not SAMPLER, reason="no sampling profiler available")
needs_tachyon = pytest.mark.skipif(not tachyon_available(), reason="other threads are only sampled with tachyon")

DJANGO_CONFTEST = """
import django
from django.conf import settings

settings.configure(
    DEBUG={debug},
    SECRET_KEY="test",
    ALLOWED_HOSTS=["*"],
    ROOT_URLCONF="urls",
    MIDDLEWARE=["whitenoise.middleware.WhiteNoiseMiddleware"],
    INSTALLED_APPS=["django.contrib.contenttypes"],
    DATABASES={{"default": {{"ENGINE": "django.db.backends.sqlite3", "NAME": ":memory:"}}}},
)
django.setup()

import pytest
from django.core.management import call_command
from django.test.utils import setup_test_environment


@pytest.fixture(scope="session", autouse=True)
def django_db_setup():
    setup_test_environment(debug={debug_in_tests})
    call_command("migrate", verbosity=0)
    from django.contrib.contenttypes.models import ContentType
    for _ in range(20):  # session setup queries, like content types: not any test's N+1
        list(ContentType.objects.filter(app_label="nothing"))
"""


def by_name(profile):
    return {test["name"]: test for test in profile["tests"]}


def ids(profile):
    return [item["id"] for item in profile["insights"]]


def django_project(pytester, *, debug=False, debug_in_tests=False):
    pytest.importorskip("django")
    pytester.makeconftest(DJANGO_CONFTEST.format(debug=debug, debug_in_tests=debug_in_tests))
    pytester.makepyfile(
        urls="""
import time
from django.http import HttpResponse
from django.urls import path

def compute(request):
    end = time.perf_counter() + 0.6
    total = 0
    while time.perf_counter() < end:
        total += sum(range(1000))
    return HttpResponse(str(total))

urlpatterns = [path("", compute)]
"""
    )
    package = pytester.mkpydir("whitenoise")
    (package / "middleware.py").write_text(
        "class WhiteNoiseMiddleware:\n"
        "    def __init__(self, get_response):\n"
        "        self.get_response = get_response\n\n"
        "    def __call__(self, request):\n"
        "        return self.get_response(request)\n"
    )


@needs_sampler
def test_a_middleware_that_wraps_the_request_doesnt_own_its_time(pytester):
    django_project(pytester)
    pytester.makepyfile(
        test_views="""
from django.test import Client

def test_view():
    assert Client().get("/").status_code == 200
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile").assert_outcomes(passed=1)
    _, profile = latest_profile(pytester)
    test = by_name(profile)["test_view"]
    assert test["categories"].get("app_code", 0) >= 0.4, test["categories"]
    # WhiteNoiseMiddleware.__call__ was on the stack for the whole 0.6s request, but none of it was its own work
    assert "django-static-storage" not in ids(profile)


def test_debug_is_read_while_tests_run(pytester):
    """pytest-django turns DEBUG off for the tests and restores it at the end of the session."""
    django_project(pytester, debug=True, debug_in_tests=False)
    pytester.makepyfile(
        test_db="""
from django.contrib.contenttypes.models import ContentType

def test_queries():
    for _ in range(5):
        list(ContentType.objects.all())
"""
    )
    pytester.makeconftest(
        pytester.path.joinpath("conftest.py").read_text()
        + """

def pytest_unconfigure(config):
    plugin = config.pluginmanager.get_plugin("flakytest-profiler")
    print("DJANGO-DEBUG", plugin._context()["django"]["debug"])
"""
    )
    result = pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none", "-s")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["DJANGO-DEBUG False"])


def test_session_setup_queries_are_the_fixtures_not_the_tests(pytester):
    django_project(pytester)
    pytester.makepyfile(
        test_db="""
from django.contrib.contenttypes.models import ContentType

def test_first():
    ContentType.objects.count()
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none").assert_outcomes(passed=1)
    _, profile = latest_profile(pytester)
    test = by_name(profile)["test_first"]
    assert test["db"]["queries"] == 1
    assert test["db"]["duplicates"] == []
    setup = next(f for f in test["fixtures"] if f["name"] == "django_db_setup")
    assert setup["db"]["duplicates"][0]["count"] >= 20  # the 20 above, and migrate's own on some versions
    assert "n-plus-one" not in ids(profile)


def test_commits_are_measured(pytester):
    """TestCase rolls back: nothing to commit. TransactionTestCase commits every write."""
    django_project(pytester)
    pytester.makepyfile(
        test_commits="""
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, TransactionTestCase

class TestRolledBack(TestCase):
    def test_writes(self):
        for i in range(10):
            ContentType.objects.create(app_label="a", model=f"m{i}")

class TestCommitted(TransactionTestCase):
    def test_writes(self):
        for i in range(10):
            ContentType.objects.create(app_label="b", model=f"m{i}")
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none").assert_outcomes(passed=2)
    _, profile = latest_profile(pytester)
    tests = {t["class"]: t for t in profile["tests"]}
    rolled_back, committed = tests["TestRolledBack"]["db"], tests["TestCommitted"]["db"]
    assert rolled_back["autocommit_writes"] == 0
    assert committed["autocommit_writes"] >= 10  # and the flush after the test
    assert committed["autocommit_write_time"] > 0


def test_one_off_fixture_cost_is_annotated(pytester):
    pytester.makeconftest(
        """
import time
import pytest

STARTED = []

@pytest.fixture(autouse=True)
def slow_start():
    if not STARTED:
        time.sleep(0.6)  # starting something once per process, like moto
        STARTED.append(1)
    yield
"""
    )
    pytester.makepyfile(
        test_suite="""
import time

def test_first():
    pass

def test_second():
    time.sleep(0.05)

def test_third():
    pass
"""
    )
    result = pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none")
    result.assert_outcomes(passed=3)
    result.stdout.fnmatch_lines(["Slowest tests:", "*test_suite.py::test_second*", "*one-off: first slow_start call*"])
    _, profile = latest_profile(pytester)
    first = by_name(profile)["test_first"]
    assert first["warmup"]["seconds"] >= 0.5
    assert first["warmup"]["causes"][0]["what"] == "first slow_start call"
    report = markdown_report(profile)
    assert "## One-off costs" in report
    slowest = report[report.index("## Slowest tests") :]
    assert slowest.index("test_second") < slowest.index("test_first")


@pytest.mark.skipif(not LINUX, reason="exact per-test memory needs Linux")
def test_memory_a_session_fixture_keeps_is_not_the_first_tests(pytester):
    pytester.makeconftest(
        """
import pytest

@pytest.fixture(scope="session")
def big_cache():
    data = bytearray(300 * 1024 * 1024)
    data[::4096] = b"x" * len(data[::4096])
    return data
"""
    )
    pytester.makepyfile(
        test_suite="""
def test_small(big_cache):
    small = bytearray(1024)
    assert len(big_cache) and small is not None

def test_other():
    pass
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none").assert_outcomes(passed=2)
    _, profile = latest_profile(pytester)
    small = by_name(profile)["test_small"]
    assert small["memory"]["peak_increase"] < 20 * MB, small["memory"]
    assert small["memory"]["growth"] < 20 * MB, small["memory"]
    cache = next(f for f in small["fixtures"] if f["name"] == "big_cache")
    assert cache["memory"] >= 290 * MB
    assert small not in memory_hogs(profile, 10)
    assert "| `big_cache` | session | 300 MB |" in markdown_report(profile)


def test_crashed_worker_is_reported_and_its_tests_kept(pytester):
    pytest.importorskip("xdist")
    pytester.makeconftest(
        """
import os

def pytest_sessionfinish(session):
    if os.environ.get("PYTEST_XDIST_WORKER") == "gw1":
        os._exit(1)
"""
    )
    pytester.makepyfile(
        test_suite="""
import time
import pytest

@pytest.mark.parametrize("i", range(8))
def test_sleep(i):
    time.sleep(0.02)
"""
    )
    result = pytester.runpytest_subprocess("--flakytest-profile", "--flakytest-backend", "none", "-n", "2")
    result.assert_outcomes(passed=8)
    result.stdout.fnmatch_lines(["*pytest-xdist workers crashed: gw1*"])
    _, profile = latest_profile(pytester)
    warning = next(w for w in profile["warnings"] if w["code"] == "missing_workers")
    assert warning["workers"] == ["gw1"]
    assert len(profile["tests"]) == 8
    assert all(test["duration"] >= 0.02 for test in profile["tests"])
    assert {test["worker"] for test in profile["tests"]} == {"gw0", "gw1"}


@needs_sampler
def test_categories_of_real_work(pytester):
    package = pytester.mkpydir("botocore")
    (package / "loaders.py").write_text(
        "import json\n\ndef load_service_model(text):\n    for _ in range(20):\n        json.loads(text)\n"
    )
    pytester.makepyfile(
        test_suite="""
import json
import socket
import threading
import time

from botocore.loaders import load_service_model

BIG = json.dumps([{"name": f"item{i}", "values": list(range(20))} for i in range(20_000)])

def test_json_in_an_http_client():
    load_service_model(BIG)

def test_socket():
    server = socket.create_server(("127.0.0.1", 0))

    def serve():
        conn, _ = server.accept()
        time.sleep(0.3)
        conn.sendall(b"hello\\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    sock = socket.create_connection(server.getsockname())
    assert sock.makefile("rb").readline() == b"hello\\n"
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile").assert_outcomes(passed=2)
    _, profile = latest_profile(pytester)
    tests = by_name(profile)
    json_test = tests["test_json_in_an_http_client"]["categories"]
    assert max(json_test, key=json_test.get) == "serialization", json_test
    assert "network" not in json_test
    socket_test = tests["test_socket"]["categories"]
    assert socket_test.get("network", 0) >= 0.2, socket_test


@needs_tachyon
def test_garbage_collection_and_thread_pools(pytester):
    pytester.makepyfile(
        test_suite="""
import gc
import time
from concurrent.futures import ThreadPoolExecutor

KEEP = [[i] for i in range(500_000)]

def test_gc():
    end = time.perf_counter() + 0.3
    while time.perf_counter() < end:
        gc.collect()

def scan_zone(_):
    end = time.perf_counter() + 0.1
    total = 0
    while time.perf_counter() < end:
        total += sum(range(1000))
    return total

def test_pool():
    with ThreadPoolExecutor(4) as pool:
        list(pool.map(scan_zone, range(12)))
"""
    )
    pytester.runpytest_subprocess("--flakytest-profile").assert_outcomes(passed=2)
    _, profile = latest_profile(pytester)
    tests = by_name(profile)
    gc_test = tests["test_gc"]["categories"]
    assert gc_test.get("gc", 0) >= 0.2, gc_test
    pool = tests["test_pool"]
    assert pool["categories"].get("waiting", 0) < 0.1 * pool["duration"], pool["categories"]
    assert pool["threads"]["count"] == 4
    hot = {profile["frames"][line["frame"]][1] for line in pool["hot_lines"]}
    assert "scan_zone" in hot
    assert any(phase for phase in pool.get("thread_stacks", {}).values())
    json.dumps(profile)
