import sysconfig
from pathlib import Path

from flakytest.profiling.frames import FrameClassifier, frame_category, stack_category

STDLIB = sysconfig.get_paths()["stdlib"]


def classify(root, filename, func="f", line=1):
    return FrameClassifier(root).classify(filename, func, line)


def test_project_files_are_test_or_app_code(tmp_path):
    assert classify(tmp_path, str(tmp_path / "tests" / "test_orders.py")).kind == "test"
    assert classify(tmp_path, str(tmp_path / "shop" / "tests.py")).kind == "test"
    assert classify(tmp_path, str(tmp_path / "conftest.py")).kind == "test"
    app = classify(tmp_path, str(tmp_path / "shop" / "views.py"))
    assert (app.kind, app.path) == ("app", "shop/views.py")


def test_installed_packages_are_shown_from_the_package(tmp_path):
    frame = classify(tmp_path, str(tmp_path / ".venv/lib/python3.15/site-packages/django/db/models/query.py"))
    assert (frame.kind, frame.path, frame.module) == ("lib", "django/db/models/query.py", "django/db/models/query.py")
    pytest_frame = classify(tmp_path, "/usr/lib/python3/dist-packages/_pytest/runner.py")
    assert pytest_frame.kind == "framework"


def test_stdlib_and_special_frames(tmp_path):
    frame = classify(tmp_path, str(Path(STDLIB) / "json" / "encoder.py"))
    assert (frame.kind, frame.path, frame.module) == ("stdlib", "<stdlib>/json/encoder.py", "json/encoder.py")
    assert classify(tmp_path, str(Path(STDLIB) / "unittest" / "case.py")).kind == "framework"
    assert classify(tmp_path, "<GC>").kind == "native"
    gc = classify(tmp_path, "~", "<GC>", 0)  # how tachyon reports the garbage collector
    assert (gc.kind, gc.module) == ("native", "<GC>")
    assert frame_category(gc.module, gc.func) == "gc"
    assert classify(tmp_path, "<frozen importlib._bootstrap>").kind == "stdlib"


def test_wrappers_count_as_framework(tmp_path):
    site = tmp_path / "venv/lib/python3.13/site-packages"
    assert classify(tmp_path, str(site / "django/test/testcases.py"), "SimpleTestCase.__call__").kind == "framework"
    assert classify(tmp_path, str(site / "django/test/testcases.py"), "TestCase._fixture_setup").kind == "lib"


def test_frame_category_matches_module_and_function():
    assert frame_category("django/contrib/auth/hashers.py", "PBKDF2PasswordHasher.encode") == "password_hashing"
    assert frame_category("django/db/models/query.py", "QuerySet.__iter__") == "db"
    assert frame_category("sqlalchemy/sql/schema.py", "MetaData.create_all") == "db_schema"
    assert frame_category("sqlalchemy/sql/schema.py", "Table.__init__") == "db"
    assert frame_category("requests/sessions.py", "Session.send") is None  # only socket I/O is network
    assert frame_category("socket.py", "SocketIO.readinto") == "network"
    assert frame_category("ssl.py", "create_default_context") is None
    assert frame_category("shop/views.py", "order_list") is None


def test_stack_category_priorities(tmp_path):
    classifier = FrameClassifier(tmp_path)
    test = classifier.classify(str(tmp_path / "tests/test_a.py"), "test_a", 3)
    hashing = classifier.classify(str(tmp_path / "venv/site-packages/django/contrib/auth/hashers.py"), "encode", 1)
    db = classifier.classify(str(tmp_path / "venv/site-packages/django/db/models/query.py"), "get", 1)
    frames = [test, db, hashing]
    hints = [frame_category(f.module, f.func) for f in frames]
    # Password hashing beats the database query that triggered it
    assert stack_category(frames, hints, "") == "password_hashing"
    assert stack_category([test], [None], "") == "test_code"
    assert stack_category([test], [None], "time.sleep(0.5)") == "sleep"


def category_of(root, *frames, source=""):
    classifier = FrameClassifier(root)
    classified = [classifier.classify(*frame) for frame in frames]
    hints = [frame_category(f.module, f.func) for f in classified]
    return stack_category(classified, hints, source)


SITE = "/venv/lib/python3.15/site-packages"


def test_http_client_work_is_not_network(tmp_path):
    test = (str(tmp_path / "tests/test_aws.py"), "test_upload", 3)
    loader = (f"{SITE}/botocore/loaders.py", "Loader.load_service_model", 400)
    decode = (str(Path(STDLIB) / "json/decoder.py"), "JSONDecoder.raw_decode", 360)
    assert category_of(tmp_path, test, loader, decode) == "serialization"
    context = (str(Path(STDLIB) / "ssl.py"), "create_default_context", 770)
    create = (f"{SITE}/botocore/httpsession.py", "create_urllib3_context", 1)
    assert category_of(tmp_path, test, create, context) == "library"
    recv = (str(Path(STDLIB) / "socket.py"), "SocketIO.readinto", 719)
    assert category_of(tmp_path, test, (f"{SITE}/urllib3/response.py", "read", 1), recv) == "network"


def test_garbage_collection_wins(tmp_path):
    test = (str(tmp_path / "tests/test_a.py"), "test_a", 3)
    query = (f"{SITE}/django/db/models/query.py", "QuerySet.get", 10)
    assert category_of(tmp_path, test, query, ("~", "<GC>", 0)) == "gc"


def test_socket_io_below_a_database_client_is_database(tmp_path):
    test = (str(tmp_path / "tests/test_a.py"), "test_a", 3)
    recv = (str(Path(STDLIB) / "socket.py"), "socket.recv_into", 1)
    assert category_of(tmp_path, test, (f"{SITE}/pymysql/connections.py", "_read_bytes", 1), recv) == "db"
    assert category_of(tmp_path, test, (f"{SITE}/redis/connection.py", "read_response", 1), recv) == "cache"


def test_context_closest_to_the_root_wins(tmp_path):
    test = (str(tmp_path / "tests/test_a.py"), "test_a", 3)
    migrate = (f"{SITE}/django/db/migrations/executor.py", "migrate", 1)
    imports = ("<frozen importlib._bootstrap>", "_find_and_load", 1)
    query = (f"{SITE}/django/db/backends/utils.py", "_execute", 1)
    assert category_of(tmp_path, test, migrate, imports, query) == "migrations"
    assert category_of(tmp_path, test, imports, migrate, query) == "imports"


def test_thread_pool_workers_are_not_waiting(tmp_path):
    worker = (str(Path(STDLIB) / "concurrent/futures/thread.py"), "_WorkItem.run", 59)
    scan = (str(tmp_path / "app/scan.py"), "scan_zone", 12)
    assert category_of(tmp_path, worker, scan) == "app_code"
    wait = (str(Path(STDLIB) / "threading.py"), "Thread._wait_for_tstate_lock", 1)
    assert category_of(tmp_path, (str(tmp_path / "tests/t.py"), "test_a", 1), wait) == "waiting"
