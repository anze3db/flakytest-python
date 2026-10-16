"""Count the SQL queries each test runs (Django and SQLAlchemy) and spot repeated ones (N+1 queries)."""

import importlib
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

NUMBER = re.compile(r"\b\d+(?:\.\d+)?\b")
STRING = re.compile(r"'(?:[^']|'')*'")
IN_LIST = re.compile(r"\bIN\s*\((?:\s*(?:%s|\?|:\w+|\$\d+)\s*,?)+\)", re.IGNORECASE)
SPACE = re.compile(r"\s+")
DUPLICATE_THRESHOLD = 3
MAX_SQL_LENGTH = 300


def normalize_sql(sql: str) -> str:
    sql = STRING.sub("?", sql)
    sql = NUMBER.sub("?", sql)
    sql = IN_LIST.sub("IN (…)", sql)
    sql = SPACE.sub(" ", sql).strip()
    return sql if len(sql) <= MAX_SQL_LENGTH else sql[: MAX_SQL_LENGTH - 1] + "…"


@dataclass
class QueryStats:
    queries: int = 0
    time: float = 0.0
    statements: Counter[tuple[str, str]] = field(default_factory=Counter)

    def add(self, sql: str, duration: float, source: str = "django") -> None:
        self.queries += 1
        self.time += duration
        self.statements[(source, normalize_sql(sql))] += 1

    def as_dict(self) -> dict[str, Any]:
        duplicates = [
            {"sql": sql, "count": count, "source": source}
            for (source, sql), count in self.statements.most_common(5)
            if count >= DUPLICATE_THRESHOLD and not sql.upper().startswith(("SAVEPOINT", "RELEASE", "ROLLBACK"))
        ]
        return {"queries": self.queries, "time": round(self.time, 6), "duplicates": duplicates}


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

    def _django_wrapper(self, execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:  # noqa: FBT001
        stats = self.current
        if stats is None:
            return execute(sql, params, many, context)
        started = time.perf_counter()
        try:
            return execute(sql, params, many, context)
        finally:
            stats.add(sql, time.perf_counter() - started)

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
