"""The code the insights look for in samples, and what the samples that match cost.

Each matcher is checked against every frame once. A sample matches a "through" matcher when any frame on its stack
matches (inclusive time: right for code that *is* the cost, like flush or commit), and an "owned" matcher when the
deepest frame that isn't the standard library, native or pytest plumbing matches. Owned is right for code that wraps
other work: a middleware's `__call__` is on the stack for the whole request, but only what it does itself (and the
standard library it calls) is its cost.

Matched totals are collected while each process builds its part of the profile and added up afterwards, so the
insights never walk the samples again.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from flakytest.profiling.frames import Frame, short_name

Spec = tuple[str, frozenset[str] | None]  # module path prefix, function names (None for any)
OWNER_SKIP_KINDS = ("stdlib", "native", "framework")


@dataclass(frozen=True)
class Matcher:
    name: str
    specs: tuple[Spec, ...]
    owned: bool = False
    categories: frozenset[str] | None = None  # only samples of these categories
    kinds: frozenset[str] | None = None  # only frames of these kinds

    def matches(self, frame: Frame) -> bool:
        if self.kinds is not None and frame.kind not in self.kinds:
            return False
        return any(
            frame.module.startswith(prefix) and (funcs is None or short_name(frame.func) in funcs)
            for prefix, funcs in self.specs
        )


def _m(
    name: str,
    *specs: str | tuple[str, tuple[str, ...]],
    owned: bool = False,
    categories: tuple[str, ...] | None = None,
    kinds: tuple[str, ...] | None = None,
) -> Matcher:
    parsed = tuple((s, None) if isinstance(s, str) else (s[0], frozenset(s[1])) for s in specs)
    return Matcher(
        name,
        parsed,
        owned=owned,
        categories=frozenset(categories) if categories else None,
        kinds=frozenset(kinds) if kinds else None,
    )


MATCHERS = [
    _m("flush", "django/core/management/commands/flush.py"),
    _m("sqlalchemy_schema", ("sqlalchemy/sql/schema.py", ("create_all", "drop_all"))),
    _m("setup_method", ("", ("setUp",)), kinds=("test",)),
    _m("containers", "testcontainers/", "docker/", owned=True),
    _m("factory_db", "factory/", "model_bakery/", categories=("db",)),
    _m("faker_instances", ("faker/proxy.py", ("__init__",)), "faker/factory.py"),
    # Hypothesis runs the whole test body: only its own work (generating data, shrinking) is its cost
    _m("hypothesis", "hypothesis/", owned=True),
    # freezegun's decorator wraps the test; starting it (scanning every module) is the cost
    _m("freezegun", "freezegun/", owned=True),
    _m("autospec", ("unittest/mock.py", ("create_autospec", "_check_sig", "_set_signature"))),
    _m("testdata_copy", ("django/test/testcases.py", ("__get__",))),
    _m("yaml", "yaml/"),
    _m(
        "db_connect",
        ("psycopg", ("connect",)),
        ("sqlalchemy/pool/", ("__connect", "_create_connection", "connect")),
        ("django/db/backends/base/base.py", ("connect",)),
        categories=("db",),
    ),
    # A commit waits for the server to flush its log: it's a leaf wait, so everything below it is its cost
    _m(
        "commit",
        ("django/db/backends/base/base.py", ("_commit", "commit")),
        ("psycopg/connection.py", ("commit",)),
        ("psycopg2/", ("commit",)),
        ("sqlalchemy/engine/base.py", ("_commit_impl",)),
    ),
    _m("debug_toolbar", "debug_toolbar/", owned=True),
    # What DEBUG adds to every query: timing it, last_executed_query() and appending it to connection.queries.
    # debug_sql is a generator that is suspended (off the stack) while the query itself runs.
    _m("debug_sql", ("django/db/backends/utils.py", ("debug_sql",))),
    # The middleware wraps the whole request, the storage is called from templates: count their own work only
    _m("static_files", "django/contrib/staticfiles/", "whitenoise/", owned=True),
    _m("celery", "celery/", "kombu/", owned=True),
    _m(
        "event_loops",
        ("asyncio/base_events.py", ("__init__", "close", "shutdown_default_executor")),
        ("asyncio/events.py", ("new_event_loop",)),
    ),
]
MATCHER_BITS = {matcher.name: 1 << index for index, matcher in enumerate(MATCHERS)}


_masks: dict[tuple[str, str, str], tuple[int, int]] = {}


def frame_masks(frame: Frame) -> tuple[int, int]:
    """(bits of the through matchers, bits of the owned matchers) that a frame matches."""
    key = (frame.module, short_name(frame.func), frame.kind)
    masks = _masks.get(key)
    if masks is None:
        masks = _masks[key] = _frame_masks(frame)
    return masks


def _frame_masks(frame: Frame) -> tuple[int, int]:
    through = owned = 0
    for index, matcher in enumerate(MATCHERS):
        if matcher.matches(frame):
            if matcher.owned:
                owned |= 1 << index
            else:
                through |= 1 << index
    return through, owned


def is_owner(frame: Frame) -> bool:
    return frame.kind not in OWNER_SKIP_KINDS


@dataclass
class Matched:
    """What the samples matching something cost: in total, per test (node id), per project line (frame id) and per
    category."""

    seconds: float = 0.0
    by_test: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))
    by_location: defaultdict[int, float] = field(default_factory=lambda: defaultdict(float))
    by_category: defaultdict[str, float] = field(default_factory=lambda: defaultdict(float))

    def add(self, nodeid: str, location: int, category: str, seconds: float) -> None:
        self.seconds += seconds
        self.by_test[nodeid] += seconds
        if location >= 0:
            self.by_location[location] += seconds
        self.by_category[category] += seconds

    def merge(self, other: "Matched", frame_ids: list[int] | None = None) -> None:
        self.seconds += other.seconds
        for nodeid, seconds in other.by_test.items():
            self.by_test[nodeid] += seconds
        for frame, seconds in other.by_location.items():
            self.by_location[frame_ids[frame] if frame_ids is not None else frame] += seconds
        for category, seconds in other.by_category.items():
            self.by_category[category] += seconds

    def as_dict(self) -> dict[str, Any]:
        return {
            "seconds": self.seconds,
            "by_test": dict(self.by_test),
            "by_location": [[frame, seconds] for frame, seconds in self.by_location.items()],
            "by_category": dict(self.by_category),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Matched":
        matched = cls(seconds=data["seconds"])
        matched.by_test.update(data["by_test"])
        matched.by_location.update((frame, seconds) for frame, seconds in data["by_location"])
        matched.by_category.update(data["by_category"])
        return matched
