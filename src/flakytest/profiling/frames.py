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
FRAMEWORK_STDLIB = (
    "unittest/case.py",
    "unittest/async_case.py",
    "unittest/suite.py",
    "unittest/main.py",
    "unittest/runner.py",
)

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

# What kind of work a frame does. Each entry: category, [(module path prefix, function names or None for any)];
# the first entry that matches a frame gives its category. How a sample's frames decide its category: see
# stack_category().
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
# Only real socket I/O is network: an HTTP client's own work (botocore loading service models, building
# requests, parsing JSON) is whatever it does, and the client frame only marks which caller made the call
_rule(
    "network",
    "socket.py",
    funcs=(
        "recv",
        "recv_into",
        "recvfrom",
        "recvfrom_into",
        "readinto",
        "send",
        "sendall",
        "sendto",
        "sendfile",
        "connect",
        "connect_ex",
        "accept",
        "create_connection",
        "getaddrinfo",
    ),
)
_rule(
    "network",
    "ssl.py",
    funcs=("read", "write", "recv", "recv_into", "send", "sendall", "do_handshake", "connect", "connect_ex"),
)
_rule("sleep", "asyncio/tasks.py", funcs=("sleep",))
_rule("waiting", "threading.py", funcs=("wait", "join", "_wait_for_tstate_lock", "acquire"))
_rule("waiting", "queue.py", funcs=("get", "put", "join"))
_rule("waiting", "selectors.py", funcs=("select", "poll"))
_rule("waiting", "concurrent/futures/", funcs=("result", "wait", "as_completed", "_result_or_cancel", "shutdown"))
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

# Categories that hold for everything below them, whatever it does (an import that queries the database is
# import time): the one closest to the root wins
CONTEXT_CATEGORIES = frozenset(
    ("imports", "password_hashing", "migrations", "db_schema", "fixture_loading", "browser", "subprocess")
)
# Clients that do their own socket I/O and waiting: network or waiting below them is theirs
OWNING_CATEGORIES = frozenset(("db", "cache"))
OWNED_CATEGORIES = frozenset(("network", "waiting"))

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
            path, kind, module = self._locate(filename, func)
            if kind in ("lib", "stdlib") and _is_wrapper(module, func):
                kind = "framework"
            frame = self._cache[key] = Frame(path=path, func=func, line=line, kind=kind, module=module)
        return frame

    def _locate(self, filename: str, func: str = "") -> tuple[str, str, str]:
        """(display path, kind, module) for a filename."""
        if filename == "~" and func.startswith("<"):  # tachyon's synthetic frames: <GC>, <native>
            return func, "native", func
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


# The category state of a stack, carried from the root to the leaf: (context, deepest other category,
# deepest owning category). NO_STATE before any frame matched.
State = tuple[str | None, str | None, str | None]
NO_STATE: State = (None, None, None)


def descend(state: State, hint: str | None) -> State:
    """The state below a frame whose frame_category() is hint."""
    if hint is None:
        return state
    context, deepest, owner = state
    if hint in CONTEXT_CATEGORIES:
        return state if context else (hint, deepest, owner)
    return context, hint, hint if hint in OWNING_CATEGORIES else owner


def resolve(state: State, leaf: Frame | None, leaf_hint: str | None, leaf_source: str) -> str:
    """A sample's category from the state at its leaf frame.

    The garbage collector (always the leaf) wins, then a sleep() on the sampled line, then the context closest
    to the root (imports, migrations, …), then the deepest resource or code category (database, network,
    serialization, …), where network or waiting below a database or cache client belongs to the client.
    """
    if leaf_hint == "gc":
        return "gc"
    if leaf_source and SLEEP_LINE.search(leaf_source):
        return "sleep"
    context, deepest, owner = state
    if context:
        return context
    if deepest:
        return owner if deepest in OWNED_CATEGORIES and owner else deepest
    if leaf is None:
        return "pytest"
    return FALLBACK_BY_KIND.get(leaf.kind, "library")


def stack_category(frames: list[Frame], hints: list[str | None], leaf_source: str) -> str:
    """The category of a sample from its frames (root first) and each frame's frame_category()."""
    state = NO_STATE
    for hint in hints:
        state = descend(state, hint)
    return resolve(state, frames[-1] if frames else None, hints[-1] if hints else None, leaf_source)
