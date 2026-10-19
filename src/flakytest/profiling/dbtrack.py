"""Count the SQL queries each test runs (Django and SQLAlchemy), spot repeated ones (N+1 queries), and measure what
durability costs: commits, and writes outside a transaction (each one commits on its own)."""

import importlib
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
STRING = re.compile(r"'(?:[^']|'')*'")
IN_LIST = re.compile(r"\bIN\s*\((?:\s*(?:%s|\?|:\w+|\$\d+)\s*,?)+\)", re.IGNORECASE)
SPACE = re.compile(r"\s+")
DUPLICATE_THRESHOLD = 3
MAX_SQL_LENGTH = 300
# Transaction and session bookkeeping, repeated by design
CONTROL_STATEMENTS = ("SAVEPOINT", "RELEASE", "ROLLBACK", "BEGIN", "COMMIT", "PRAGMA", "SET ", "SHOW ")
WRITES = ("INSERT", "UPDATE", "DELETE", "insert", "update", "delete")


@lru_cache(maxsize=16384)
def normalize_sql(sql: str) -> str:
    sql = STRING.sub("?", sql)
    sql = NUMBER.sub("?", sql)
    sql = IN_LIST.sub("IN (…)", sql)
    sql = SPACE.sub(" ", sql).strip()
    return sql if len(sql) <= MAX_SQL_LENGTH else sql[: MAX_SQL_LENGTH - 1] + "…"


def is_control(sql: str) -> bool:
    return sql[:16].lstrip().upper().startswith(CONTROL_STATEMENTS)


@dataclass
class QueryStats:
    queries: int = 0
    time: float = 0.0
    # Raw SQL is counted as it comes (ORMs send parameterised SQL, so repeats are the same string) and normalized
    # only when the test is over, once per distinct statement
    statements: Counter[tuple[str, str]] = field(default_factory=Counter)
    commits: int = 0
    commit_time: float = 0.0
    autocommit_writes: int = 0
    autocommit_write_time: float = 0.0
    measures_commits: bool = False

    def add(self, sql: str, duration: float, source: str = "django") -> None:
        self.queries += 1
        self.time += duration
        if not is_control(sql):
            self.statements[(source, sql)] += 1

    def add_commit(self, duration: float) -> None:
        self.commits += 1
        self.commit_time += duration

    def add_autocommit_write(self, duration: float) -> None:
        self.autocommit_writes += 1
        self.autocommit_write_time += duration

    def as_dict(self) -> dict[str, Any]:
        normalized: Counter[tuple[str, str]] = Counter()
        for (source, sql), count in self.statements.items():
            normalized[(source, normalize_sql(sql))] += count
        duplicates = [
            {"sql": sql, "count": count, "source": source}
            for (source, sql), count in normalized.most_common()
            if count >= DUPLICATE_THRESHOLD
        ][:5]
        result: dict[str, Any] = {"queries": self.queries, "time": round(self.time, 6), "duplicates": duplicates}
        if self.measures_commits:
            result |= {
                "commits": self.commits,
                "commit_time": round(self.commit_time, 6),
                "autocommit_writes": self.autocommit_writes,
                "autocommit_write_time": round(self.autocommit_write_time, 6),
            }
        return result


class QueryTracker:
    """Hooks into Django connections and SQLAlchemy engines once the project has imported them.

    Both are optional, so their modules are looked up with importlib instead of imported up front, which
    would slow down (or break) every project that doesn't use them.
    """

    def __init__(self) -> None:
        self.current: QueryStats | None = None
        self.django = False
        self.sqlalchemy = False

    def install(self) -> None:
        if not self.django and "django.db" in sys.modules:
            self.django = self._install_django()
        if self.django:
            self._wrap_django_connections()
        if not self.sqlalchemy and "sqlalchemy" in sys.modules:
            self.sqlalchemy = self._install_sqlalchemy()

    def _install_django(self) -> bool:
        try:
            settings = importlib.import_module("django.conf").settings
            if not settings.configured:
                return False
            signals = importlib.import_module("django.db.backends.signals")
        except Exception:
            return False
        signals.connection_created.connect(self._on_django_connection, weak=False)
        return True

    def _wrap_django_connections(self) -> None:
        connections = sys.modules["django.db"].connections
        try:
            initialized = connections.all(initialized_only=True)
        except TypeError:  # Django < 4.1
            initialized = [connections[alias] for alias in connections]
        for connection in initialized:
            self._on_django_connection(connection=connection)

    def _on_django_connection(self, connection: Any = None, **_: Any) -> None:
        wrappers = getattr(connection, "execute_wrappers", None)
        if wrappers is not None and self._django_wrapper not in wrappers:
            wrappers.append(self._django_wrapper)
        # Django commits through connection.commit(), not a cursor, so execute wrappers never see it
        commit = getattr(connection, "_commit", None)
        if commit is not None and not getattr(commit, "_flakytest", False):
            tracker = self

            def timed_commit(*args: Any, **kwargs: Any) -> Any:
                stats = tracker.current
                if stats is None:
                    return commit(*args, **kwargs)
                started = time.perf_counter()
                try:
                    return commit(*args, **kwargs)
                finally:
                    stats.add_commit(time.perf_counter() - started)

            timed_commit._flakytest = True  # type: ignore[attr-defined]
            connection._commit = timed_commit

    def _django_wrapper(self, execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:  # noqa: FBT001
        stats = self.current
        if stats is None:
            return execute(sql, params, many, context)
        stats.measures_commits = True
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            duration = time.perf_counter() - started
            stats.add(sql, duration)
            if sql.lstrip().startswith(WRITES):
                connection = context.get("connection") if isinstance(context, dict) else None
                if connection is not None and connection.get_autocommit() and not connection.in_atomic_block:
                    stats.add_autocommit_write(duration)

    def _install_sqlalchemy(self) -> bool:
        try:
            event = importlib.import_module("sqlalchemy.event")
            engine = importlib.import_module("sqlalchemy.engine").Engine
        except Exception:
            return False
        event.listen(engine, "before_cursor_execute", self._before_sqlalchemy)
        event.listen(engine, "after_cursor_execute", self._after_sqlalchemy)
        return True

    def _before_sqlalchemy(self, conn: Any, *_: Any) -> None:
        conn.info.setdefault("flakytest_started", []).append(time.perf_counter())

    def _after_sqlalchemy(self, conn: Any, _cursor: Any, statement: str, *_: Any) -> None:
        started = conn.info.get("flakytest_started")
        duration = time.perf_counter() - started.pop() if started else 0.0
        if self.current is not None:
            self.current.add(statement, duration, "sqlalchemy")
