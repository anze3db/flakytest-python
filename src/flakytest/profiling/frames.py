"""Classify frames: where the code lives (kind), what kind of work it does (category), how to show it."""

import linecache
import os
import re
import sysconfig
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

PACKAGE_DIRS = ("site-packages", "dist-packages")
FRAMEWORK_PACKAGES = {"_pytest", "pytest", "pluggy", "py", "xdist", "execnet", "flakytest", "pytest_asyncio"}
FRAMEWORK_STDLIB = ("unittest/case.py", "unittest/suite.py", "unittest/main.py", "unittest/runner.py")

# Frames that only wrap the real work: classified as "framework" so segments and call trees skip them
TRANSPARENT = {
    "django/test/testcases.py": {"__call__", "_setup_and_call", "run", "debug", "_callTestMethod", "_run_signature"},
    "django/test/utils.py": {"inner", "decorate_callable"},
    "contextlib.py": {"inner", "__enter__", "__exit__"},
    "functools.py": {"wrapper"},
    "unittest/mock.py": {"patched"},
    "asyncio/runners.py": None,
    "asyncio/base_events.py": {"run_until_complete", "run_forever", "_run_once"},
    "asyncio/events.py": {"_run"},
    "anyio/pytest_plugin.py": None,
    "pytest_django/plugin.py": {"non_debugging_runtest", "_django_setup_unittest"},
    "anyio/_backends/_asyncio.py": None,
}

# Checked in this order against every frame of a sample, the first category that matches wins.
# Each entry: category, [(module path prefix, function names or None for any)]
CATEGORY_RULES: list[tuple[str, list[tuple[str, frozenset[str] | None]]]] = []


def _rule(category: str, *prefixes: str, funcs: tuple[str, ...] | None = None) -> None:
    entries = [(prefix, frozenset(funcs) if funcs else None) for prefix in prefixes]
    for existing_category, existing in CATEGORY_RULES:
        if existing_category == category:
            existing.extend(entries)
            return
    CATEGORY_RULES.append((category, entries))


_rule("gc", "<GC>")
_rule("imports", "<frozen importlib._bootstrap>", "<frozen importlib._bootstrap_external>", "importlib/")
_rule(
    "password_hashing",
    "django/contrib/auth/hashers.py",
    "bcrypt/",
    "argon2/",
    "passlib/",
    "pwdlib/",
    "werkzeug/security.py",
)
_rule("password_hashing", "hashlib.py", funcs=("pbkdf2_hmac", "scrypt"))
_rule("migrations", "django/db/migrations/", "alembic/")
_rule(
    "db_schema",
    "django/core/management/commands/flush.py",
    "django/core/management/commands/migrate.py",
    "django/db/backends/base/creation.py",
    "django/db/backends/postgresql/creation.py",
    "django/db/backends/sqlite3/creation.py",
    "django/db/backends/mysql/creation.py",
    "sqlalchemy/sql/ddl.py",
)
_rule("db_schema", "sqlalchemy/sql/schema.py", funcs=("create_all", "drop_all"))
_rule("fixture_loading", "django/core/management/commands/loaddata.py", "django/core/serializers/")
_rule("browser", "selenium/", "playwright/", "splinter/", "pyppeteer/")
_rule("subprocess", "subprocess.py", "asyncio/subprocess.py", "multiprocessing/", "testcontainers/", "docker/")
_rule(
    "db",
    "django/db/",
    "psycopg/",
    "psycopg2/",
    "psycopg_pool/",
    "sqlite3/",
    "sqlalchemy/",
    "sqlmodel/",
    "asyncpg/",
    "aiosqlite/",
    "pymysql/",
    "MySQLdb/",
    "mysql/connector/",
    "pymongo/",
    "motor/",
    "peewee.py",
    "tortoise/",
    "databases/",
    "oracledb/",
    "piccolo/",
)
_rule("cache", "redis/", "valkey/", "pymemcache/", "pylibmc/", "django/core/cache/", "django_redis/")
_rule(
    "network",
    "socket.py",
    "ssl.py",
    "http/client.py",
    "urllib/request.py",
    "urllib3/",
    "requests/",
    "httpx/",
    "httpcore/",
    "aiohttp/",
    "botocore/",
    "boto3/",
    "grpc/",
    "websockets/",
    "h11/",
    "smtplib.py",
    "dns/",
)
_rule("sleep", "asyncio/tasks.py", funcs=("sleep",))
_rule("waiting", "threading.py", funcs=("wait", "join", "_wait_for_tstate_lock", "acquire"))
_rule("waiting", "queue.py", funcs=("get", "put", "join"))
_rule("waiting", "selectors.py", "concurrent/futures/")
_rule("template", "django/template/", "jinja2/", "mako/", "chameleon/")
_rule("factories", "factory/", "model_bakery/", "faker/", "mixer/", "polyfactory/", "hypothesis/", "mimesis/")
_rule("mocking", "unittest/mock.py", "pytest_mock/", "freezegun/", "time_machine/", "responses/", "respx/")
_rule("copying", "copy.py")
_rule(
    "serialization",
    "json/",
    "pickle.py",
    "pydantic/",
    "pydantic_core/",
    "marshmallow/",
    "rest_framework/serializers.py",
    "rest_framework/fields.py",
    "yaml/",
    "xml/",
)
_rule("logging", "logging/")

CATEGORY_ORDER = [category for category, _ in CATEGORY_RULES] + [
    "test_code",
    "app_code",
    "pytest",
    "library",
]
SLEEP_LINE = re.compile(r"\bsleep\s*\(")
FALLBACK_BY_KIND = {"test": "test_code", "app": "app_code", "framework": "pytest"}


@dataclass(frozen=True)
class Frame:
    path: str  # what to show: relative to the rootdir, the package, or <stdlib>/
    func: str
    line: int
    kind: str  # test, app, lib, stdlib, framework, native
    module: str  # path used for matching rules: django/db/models/query.py, json/encoder.py, <GC>


def short_name(func: str) -> str:
    """The function name without its class: tachyon reports qualified names (Class.method), py-spy doesn't."""
    return func.rsplit(".", 1)[-1]


def _is_test_file(rel: str) -> bool:
    name = os.path.basename(rel)
    if name in ("tests.py", "conftest.py") or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(part in ("tests", "test", "testing") for part in Path(rel).parts[:-1])


class FrameClassifier:
    def __init__(self, rootdir: Path) -> None:
        self.rootdir = str(rootdir.resolve())
        paths = sysconfig.get_paths()
        self.stdlib_dirs = sorted(
            {str(Path(paths[key]).resolve()) for key in ("stdlib", "platstdlib") if key in paths},
            key=len,
            reverse=True,
        )
        self.home = str(Path.home())
        self._cache: dict[tuple[str, str, int], Frame] = {}

    def classify(self, filename: str, func: str, line: int) -> Frame:
        key = (filename, func, line)
        frame = self._cache.get(key)
        if frame is None:
            path, kind, module = self._locate(filename)
            if kind in ("lib", "stdlib") and _is_wrapper(module, func):
                kind = "framework"
            frame = self._cache[key] = Frame(path=path, func=func, line=line, kind=kind, module=module)
        return frame

    def _locate(self, filename: str) -> tuple[str, str, str]:
        """(display path, kind, module) for a filename."""
        if not filename or filename in ("~", "<GC>", "<native>") or filename.startswith("<native"):
            return filename or "<unknown>", "native", filename or "<unknown>"
        if filename.startswith("<frozen "):
            return filename, "stdlib", filename
        if filename.startswith("<"):
            return filename, "lib", filename
        normalized = filename.replace("\\", "/")
        for marker in PACKAGE_DIRS:
            index = normalized.rfind(f"/{marker}/")
            if index != -1:
                rel = normalized[index + len(marker) + 2 :]
                top = rel.split("/", 1)[0]
                return rel, "framework" if top in FRAMEWORK_PACKAGES else "lib", rel
        for stdlib in self.stdlib_dirs:
            if normalized.startswith(stdlib.replace("\\", "/") + "/"):
                rel = normalized[len(stdlib) + 1 :]
                kind = "framework" if rel.startswith(FRAMEWORK_STDLIB) else "stdlib"
                return f"<stdlib>/{rel}", kind, rel
        root = self.rootdir.replace("\\", "/")
        if normalized.startswith(root + "/"):
            rel = normalized[len(root) + 1 :]
            return rel, "test" if _is_test_file(rel) else "app", rel
        if normalized.startswith(self.home):
            return "~" + normalized[len(self.home) :], "lib", normalized
        return normalized, "lib", normalized


@lru_cache(maxsize=65536)
def frame_category(module: str, func: str) -> str | None:
    for category, entries in CATEGORY_RULES:
        for prefix, funcs in entries:
            if module.startswith(prefix) and (funcs is None or short_name(func) in funcs):
                return category
    return None


def _is_wrapper(module: str, func: str) -> bool:
    funcs = TRANSPARENT.get(module, False)
    if funcs is False:
        return False
    return funcs is None or short_name(func) in funcs  # type: ignore[operator]


def is_transparent(frame: Frame) -> bool:
    """Plumbing between the test runner and the real work: pytest, unittest, decorators, event loops."""
    return frame.kind in ("framework", "native")


def source_line(filename: str, line: int) -> str:
    if line <= 0 or not filename or filename.startswith("<"):
        return ""
    return linecache.getline(filename, line).strip()


def stack_category(frames: list[Frame], hints: list[str | None], leaf_source: str) -> str:
    """The category of a sample from its frames (root first) and each frame's frame_category()."""
    if leaf_source and SLEEP_LINE.search(leaf_source):
        return "sleep"
    best = None
    best_rank = len(CATEGORY_ORDER)
    for hint in hints:
        if hint is not None:
            rank = CATEGORY_RANK[hint]
            if rank < best_rank:
                best, best_rank = hint, rank
    if best is not None:
        return best
    if not frames:
        return "pytest"
    return FALLBACK_BY_KIND.get(frames[-1].kind, "library")


CATEGORY_RANK = {category: rank for rank, category in enumerate(CATEGORY_ORDER)}
