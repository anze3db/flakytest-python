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
    assert frame_category("requests/sessions.py", "Session.send") == "network"
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
