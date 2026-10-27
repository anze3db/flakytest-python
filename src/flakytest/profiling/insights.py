"""Fix-next suggestions: perf tricks for pytest, Django and SQLAlchemy, backed by what the profile measured.

Every rule reads the profile document (and a little context the plugin gathers, like Django settings),
estimates how many seconds per run the problem costs, and explains the fix and how to keep it safe.
"""

from collections import defaultdict
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from flakytest.profiling.frames import source_line
from flakytest.profiling.matchers import Matched

MAX_TESTS = 20
MAX_LOCATIONS = 10
DJANGO_TESTING_DOCS = "https://docs.djangoproject.com/en/stable/topics/testing/overview/"
DJANGO_SPEED_DOCS = "https://docs.djangoproject.com/en/stable/topics/testing/overview/#speeding-up-the-tests"


class Profile:
    """Read helpers over a profile document and what its samples matched (see matchers.py)."""

    def __init__(
        self,
        doc: dict[str, Any],
        context: dict[str, Any],
        matched: dict[str, Matched] | None = None,
        rootdir: Path | None = None,
    ) -> None:
        self.doc = doc
        self.context = context
        self.matched = matched or {}
        self.rootdir = rootdir
        self.tests = doc["tests"]
        self.frames = doc["frames"]
        self.test_time = doc["summary"]["test_time"] or 0.0
        self.wall = doc["run"].get("duration") or self.test_time

    def in_category(self, *categories: str) -> "Found":
        found = Matched()
        for category in categories:
            if (matched := self.matched.get("category:" + category)) is not None:
                found.merge(matched)
        return Found(self, found)

    def through(self, name: str) -> "Found":
        """The samples a matcher from matchers.MATCHERS matched."""
        return Found(self, self.matched.get(name) or Matched())

    def location(self, frame_id: int) -> dict[str, Any]:
        path, func, line, _ = self.frames[frame_id]
        return {"frame": frame_id, "location": f"{path}:{line}", "function": func}

    def source(self, frame_id: int) -> str:
        text = self.doc.get("sources", {}).get(str(frame_id), "")
        if not text and self.rootdir is not None:
            path, _, line, kind = self.frames[frame_id]
            if kind in ("test", "app"):
                text = source_line(str(self.rootdir / path), line)
        return text


class Found:
    def __init__(self, profile: Profile, matched: Matched) -> None:
        self.profile = profile
        self.matched = matched
        self.seconds = matched.seconds
        self.by_test = matched.by_test
        self.by_category = matched.by_category

    def tests(self) -> list[str]:
        return [nodeid for nodeid, _ in top(self.by_test, MAX_TESTS)]

    def locations(self) -> list[dict[str, Any]]:
        return [
            self.profile.location(frame_id) | {"seconds": round(seconds, 6)}
            for frame_id, seconds in top(self.matched.by_location, MAX_LOCATIONS)
        ]


def top(values: dict[Any, float], n: int) -> list[tuple[Any, float]]:
    return sorted(values.items(), key=lambda item: -item[1])[:n]


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def pct(seconds: float, total: float) -> str:
    return f"{seconds / total:.0%}" if total else "n/a"


def fmt(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f} min"
    if seconds >= 1:
        return f"{seconds:.1f}s"
    return f"{seconds * 1000:.0f}ms"


def severity(impact: float, total: float) -> str:
    share = impact / total if total else 0
    if share >= 0.10 or impact >= 60:
        return "high"
    if share >= 0.03 or impact >= 10:
        return "medium"
    return "low"


def insight(
    profile: Profile,
    id: str,  # noqa: A002
    title: str,
    *,
    category: str,
    impact: float,
    summary: str,
    fix: str,
    safety: str = "",
    docs: str = "",
    matched: Found | None = None,
    tests: list[str] | None = None,
    evidence: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "category": category,
        "severity": severity(impact, profile.test_time),
        "impact": round(impact, 3),
        "summary": summary,
        "fix": fix,
        "safety": safety,
        "docs": docs,
        "tests": tests if tests is not None else (matched.tests() if matched else []),
        "locations": matched.locations() if matched else [],
        "evidence": evidence or [],
    }


def worth_it(seconds: float, profile: Profile, minimum: float = 0.5, share: float = 0.02) -> bool:
    return seconds >= minimum and seconds >= profile.test_time * share


Rule = Callable[[Profile], Iterator[dict[str, Any]]]
RULES: list[Rule] = []


def rule(fn: Rule) -> Rule:
    RULES.append(fn)
    return fn


def django(profile: Profile) -> dict[str, Any]:
    return profile.context.get("django") or {}


def runner(profile: Profile) -> str:
    """What ran the tests: pytest, django (manage.py test) or unittest."""
    return profile.context.get("runner") or "pytest"


@rule
def password_hashing(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("password_hashing")
    if not worth_it(matched.seconds, profile, 0.3, 0.01):
        return
    logins = [
        loc["location"]
        for loc in matched.locations()
        if ".login(" in profile.source(loc["frame"]) or "authenticate(" in profile.source(loc["frame"])
    ]
    settings = django(profile)
    if settings:
        hasher = (settings.get("password_hashers") or ["?"])[0]
        fix = (
            'In your test settings set PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"] '
            f"(currently {hasher.rsplit('.', 1)[-1]})."
        )
        if logins:
            fix += (
                " Also replace self.client.login(username=…, password=…) with self.client.force_login(user), "
                "which skips password checks entirely: " + ", ".join(logins[:3])
            )
    else:
        fix = (
            "Use the cheapest work factor in tests: bcrypt.gensalt(rounds=4), argon2 PasswordHasher(time_cost=1, "
            "memory_cost=8, parallelism=1), or a passlib CryptContext configured with minimal rounds for tests."
        )
    yield insight(
        profile,
        "password-hashing",
        "Use a fast password hasher in tests",
        category="password_hashing",
        impact=matched.seconds * 0.95,
        summary=f"{fmt(matched.seconds)} ({pct(matched.seconds, profile.test_time)} of test time) is spent hashing "
        "passwords. Slow hashing protects real passwords; in tests it only burns CPU.",
        fix=fix,
        safety="Only change the hasher in test settings. Tests that assert on a specific hash format need updating.",
        docs=f"{DJANGO_TESTING_DOCS}#password-hashing" if settings else "",
        matched=matched,
    )


def migrations_time(profile: Profile, seconds: float) -> str:
    """With pytest-xdist every worker migrates its own test database, in parallel: the sum isn't time anyone waits."""
    workers = profile.doc["run"].get("workers") or 1
    if workers > 1 and runner(profile) == "pytest":  # Django's --parallel workers clone the migrated database
        return f"{fmt(seconds)} across {workers} pytest-xdist workers (about {fmt(seconds / workers)} each)"
    return f"{fmt(seconds)} per run"


@rule
def migrations(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("migrations")
    if not worth_it(matched.seconds, profile, 1.0, 0.01):
        return
    pytest = profile.context.get("pytest") or {}
    if pytest.get("pytest_django"):
        fix = (
            "Run with --reuse-db (add it to addopts in pytest.ini/pyproject) so the test database survives "
            "between runs; pass --create-db when migrations change. If no test depends on data migrations, "
            "--nomigrations builds the schema straight from the models, which is faster still."
        )
    elif django(profile):
        fix = "Run manage.py test --keepdb so the test database and its migrations are reused between runs."
    else:
        fix = (
            "Create the schema once per session (metadata.create_all or a single alembic upgrade in a "
            "session-scoped fixture) and keep the database between runs instead of migrating every time."
        )
    yield insight(
        profile,
        "reuse-test-database",
        "Stop re-running migrations on every test run",
        category="migrations",
        impact=matched.seconds * 0.9,
        summary=f"Migrations take {migrations_time(profile, matched.seconds)}, before the first test can start.",
        fix=fix,
        safety="CI should still build the database from scratch (no --reuse-db or --keepdb) so broken migrations "
        "are caught.",
        docs="https://pytest-django.readthedocs.io/en/latest/database.html"
        "#reuse-db-reuse-the-testing-database-between-test-runs"
        if pytest.get("pytest_django")
        else DJANGO_SPEED_DOCS,
        matched=matched,
    )


@rule
def transactional_flush(profile: Profile) -> Iterator[dict[str, Any]]:
    flush = profile.through("flush")
    if worth_it(flush.seconds, profile, 0.5, 0.01):
        yield insight(
            profile,
            "django-transaction-testcase",
            "Use TestCase instead of TransactionTestCase",
            category="db_schema",
            impact=flush.seconds * 0.9,
            summary=f"{fmt(flush.seconds)} goes to flushing every table after tests in {len(flush.by_test)} "
            "TransactionTestCase / transaction=True / live_server tests.",
            fix="Use django.test.TestCase (or @pytest.mark.django_db without transaction=True): it wraps each test "
            "in a transaction and rolls back instead of truncating every table. Keep TransactionTestCase only "
            "for tests that need real commits (on_commit hooks can use TestCase.captureOnCommitCallbacks).",
            safety="Tests that rely on commit behaviour, multiple connections or a live server still need a "
            "transactional test case.",
            docs="https://docs.djangoproject.com/en/stable/topics/testing/tools/#transactiontestcase",
            matched=flush,
        )
    schema = profile.through("sqlalchemy_schema")
    if worth_it(schema.seconds, profile, 0.5, 0.01) and len(schema.by_test) > 1:
        yield insight(
            profile,
            "sqlalchemy-schema-per-test",
            "Create the SQLAlchemy schema once, roll back per test",
            category="db_schema",
            impact=schema.seconds * 0.9,
            summary=f"metadata.create_all / drop_all runs in {len(schema.by_test)} tests and costs "
            f"{fmt(schema.seconds)}.",
            fix="Create the schema in a session-scoped fixture, then give every test a connection with an outer "
            "transaction and bind the Session with join_transaction_mode='create_savepoint'; roll back at the "
            "end of each test.",
            safety="Code that commits still works: the commit releases a savepoint, and the outer rollback undoes it.",
            docs="https://docs.sqlalchemy.org/en/20/orm/session_transaction.html"
            "#joining-a-session-into-an-external-transaction-such-as-for-test-suites",
            matched=schema,
        )


@rule
def fixture_loading(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("fixture_loading")
    if not worth_it(matched.seconds, profile, 1.0):
        return
    yield insight(
        profile,
        "django-fixture-loading",
        "Load test data once per class, not once per test",
        category="fixture_loading",
        impact=matched.seconds * 0.7,
        summary=f"Loading Django fixtures (loaddata / deserialization) costs {fmt(matched.seconds)}.",
        fix="With TestCase, class-level `fixtures = [...]` load once per class; with TransactionTestCase or "
        "pytest-django's django_db_blocker they load per test. Prefer setUpTestData with factories, or load "
        "shared reference data once in a session fixture (django_db_setup) with --reuse-db.",
        safety="Data loaded once per class is rolled back per test only inside TestCase's transaction.",
        docs="https://docs.djangoproject.com/en/stable/topics/testing/tools/#fixture-loading",
        matched=matched,
    )


@rule
def setup_per_test(profile: Profile) -> Iterator[dict[str, Any]]:
    """Django TestCase classes that build database rows in setUp for every test."""
    classes: defaultdict[tuple[str, str], list[str]] = defaultdict(list)
    for test in profile.tests:
        if test.get("class"):
            classes[(test["file"], test["class"])].append(test["nodeid"])
    matched = profile.through("setup_method")
    if not worth_it(matched.seconds, profile, 0.5, 0.02):
        return
    mostly = ", ".join(
        f"{category.replace('_', ' ')} {fmt(seconds)}" for category, seconds in top(matched.by_category, 3)
    )
    multi = [
        f"{cls} ({len(indexes)} tests)"
        for (file, cls), indexes in classes.items()
        if len(indexes) > 1 and any(matched.by_test.get(nodeid) for nodeid in indexes)
    ]
    if not multi:
        return
    yield insight(
        profile,
        "django-setuptestdata",
        "Create shared test data in setUpTestData instead of setUp",
        category="db",
        impact=matched.seconds * 0.7,
        summary=f"setUp() runs before every test and takes {fmt(matched.seconds)} in total ({mostly}) in "
        f"{len(multi)} {'class' if len(multi) == 1 else 'classes'}: {', '.join(multi[:4])}.",
        fix="Move object creation that tests only read from setUp() into `@classmethod def setUpTestData(cls)`: it "
        "runs once per class and each test still sees the original rows (TestCase rolls back to a savepoint).",
        safety="Since Django 3.2 attributes assigned in setUpTestData are deep-copied per test, so in-memory changes "
        "don't leak between tests. Keep objects a test modifies in the database (or re-fetches) as before.",
        docs="https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TestCase.setUpTestData",
        matched=matched,
    )


@rule
def expensive_function_fixtures(profile: Profile) -> Iterator[dict[str, Any]]:
    for fixture in profile.doc.get("fixtures", []):
        if fixture["scope"] != "function" or fixture["calls"] < 5:
            continue
        if fixture["mean"] < 0.05 or not worth_it(fixture["total"], profile, 1.0, 0.03):
            continue
        yield insight(
            profile,
            f"fixture-scope-{fixture['name']}",
            f"Fixture `{fixture['name']}` is rebuilt for every test",
            category="fixtures",
            impact=fixture["total"] * 0.8,
            summary=f"`{fixture['name']}` is function-scoped: it ran {fixture['calls']} times at "
            f"{fmt(fixture['mean'])} each, {fmt(fixture['total'])} in total.",
            fix=f"If tests only read what `{fixture['name']}` builds, give it scope='module' or scope='session'. "
            "If they modify it, split it: an expensive higher-scoped fixture that builds the thing once, plus a cheap "
            "function-scoped one that resets or copies it (or wraps it in a transaction that is rolled back).",
            safety="A wider scope shares state between tests: check no test mutates it, and run the suite in random "
            "order (pytest-randomly) to catch leaks.",
            docs="https://docs.pytest.org/en/stable/how-to/fixtures.html#scope-sharing-fixtures-across-classes-modules"
            "-packages-or-session",
            tests=[t["nodeid"] for t in profile.tests if any(f["name"] == fixture["name"] for f in t["fixtures"])][
                :MAX_TESTS
            ],
            evidence=[f"max {fmt(fixture['max'])}, used by {fixture['tests']} tests"],
        )


@rule
def sleeping(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("sleep")
    if not worth_it(matched.seconds, profile, 0.2, 0.01):
        return
    yield insight(
        profile,
        "sleep",
        "Tests are sleeping",
        category="sleep",
        impact=matched.seconds * 0.9,
        summary=f"{fmt(matched.seconds)} of test time is spent in sleep() calls, in "
        f"{plural(len(matched.by_test), 'test')}.",
        fix="Don't wait a fixed time for something to happen: poll for the condition with a short interval and a "
        "timeout, patch time.sleep in retry/backoff code (or make the delay configurable and 0 in tests), and "
        "use time-machine or freezegun to move the clock instead of waiting for it.",
        safety="Polling keeps the test meaningful; a mocked sleep in retry code should still assert how often it "
        "retried.",
        matched=matched,
    )


@rule
def waiting(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("waiting")
    if not worth_it(matched.seconds, profile, 1.0, 0.03):
        return
    yield insight(
        profile,
        "waiting",
        "Tests are blocked waiting on threads, queues or the event loop",
        category="waiting",
        impact=matched.seconds * 0.5,
        summary=f"{fmt(matched.seconds)} is spent blocked in Event.wait / Thread.join / Queue.get / select().",
        fix="Look at the listed lines: lower timeouts in test settings, signal completion instead of waiting for a "
        "timeout, and run background work synchronously in tests (e.g. CELERY_TASK_ALWAYS_EAGER, a synchronous "
        "executor).",
        matched=matched,
    )


@rule
def network(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("network")
    if not worth_it(matched.seconds, profile, 0.3, 0.01):
        return
    yield insight(
        profile,
        "network",
        "Tests make real network calls",
        category="network",
        impact=matched.seconds * 0.9,
        summary=f"{fmt(matched.seconds)} is spent on sockets/HTTP in {plural(len(matched.by_test), 'test')}. "
        "Network calls are slow, and the top cause of flaky tests that pass locally but fail in CI (or vice versa).",
        fix="Mock HTTP at the transport: responses or pytest-httpserver for requests, respx or pytest-httpx for "
        "httpx, aioresponses for aiohttp, botocore Stubber or moto for AWS; record real responses with "
        "vcrpy/pytest-recording. Then block accidental network access with pytest-socket (--disable-socket "
        "--allow-unix-socket).",
        safety="Keep a few explicitly marked integration tests that talk to the real service.",
        matched=matched,
    )


@rule
def cache_backend(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("cache")
    if not worth_it(matched.seconds, profile, 0.5, 0.01):
        return
    settings = django(profile)
    if settings and any("redis" in b.lower() or "memcache" in b.lower() for b in settings.get("cache_backends", [])):
        fix = (
            'Use CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}} in test '
            "settings, or fakeredis for code that needs Redis features."
        )
    else:
        fix = "Use an in-memory cache in tests (locmem / fakeredis) unless the test is about the cache itself."
    yield insight(
        profile,
        "cache-backend",
        "Use an in-memory cache in tests",
        category="cache",
        impact=matched.seconds * 0.8,
        summary=f"{fmt(matched.seconds)} is spent talking to Redis/Memcached.",
        fix=fix,
        safety="Clear the cache between tests (cache.clear() in an autouse fixture) so tests stay independent.",
        matched=matched,
    )


@rule
def subprocesses(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("subprocess")
    if not worth_it(matched.seconds, profile, 1.0, 0.02):
        return
    containers = profile.through("containers").seconds
    fix = (
        "Call the Python code directly instead of spawning a process (click.testing.CliRunner, "
        "django.core.management.call_command, calling main(argv)), or mock subprocess.run for tests that only "
        "check the command line."
    )
    if containers:
        fix += " Start containers once per session (session-scoped fixture) and reset their state per test."
    yield insight(
        profile,
        "subprocess",
        "Tests spawn processes",
        category="subprocess",
        impact=matched.seconds * 0.7,
        summary=f"{fmt(matched.seconds)} is spent starting and waiting for subprocesses.",
        fix=fix,
        safety="Keep one end-to-end test that runs the real command.",
        matched=matched,
    )


@rule
def browsers(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("browser")
    if not worth_it(matched.seconds, profile, 2.0, 0.05):
        return
    yield insight(
        profile,
        "browser",
        "Share the browser between tests",
        category="browser",
        impact=matched.seconds * 0.4,
        summary=f"Browser automation takes {fmt(matched.seconds)} in {len(matched.by_test)} tests.",
        fix="Launch the browser once per session and give each test a fresh context/page (pytest-playwright does "
        "this by default; with Selenium use a session-scoped driver and delete cookies per test). Run browser "
        "tests in their own CI job, in parallel, and cover logic with fast unit tests.",
        safety="A fresh browser context per test keeps cookies and storage isolated.",
        matched=matched,
    )


@rule
def factories(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("factories")
    db_from_factories = profile.through("factory_db")
    total = matched.seconds + db_from_factories.seconds
    if not worth_it(total, profile, 1.0, 0.03):
        return
    faker = profile.through("faker_instances").seconds
    hypothesis = profile.through("hypothesis").seconds
    fix = [
        "Create objects tests only read once: setUpTestData, or a module/session-scoped fixture.",
        (
            "Use build()/stub() instead of create() (factory_boy) or baker.prepare() (model_bakery) when the "
            "test doesn't need rows in the database."
        ),
        (
            "Create many objects at once: baker.make(Model, _quantity=n, _bulk_create=True) or "
            "Factory.create_batch with bulk_create."
        ),
    ]
    if faker >= 0.2:
        fix.append(f"Creating Faker() instances costs {fmt(faker)}: create one at module level and reuse it.")
    if hypothesis >= 1.0:
        fix.append(
            f"Hypothesis takes {fmt(hypothesis)}: register a 'dev' settings profile with fewer max_examples for "
            "local runs and keep the default in CI."
        )
    yield insight(
        profile,
        "factories",
        "Test data factories are a large share of test time",
        category="factories",
        impact=total * 0.5,
        summary=f"Factories and fake data take {fmt(matched.seconds)}, plus {fmt(db_from_factories.seconds)} of "
        "database writes they trigger.",
        fix=" ".join(fix),
        safety="Shared objects must not be modified by tests (or must live inside TestCase's rollback).",
        matched=matched if matched.seconds >= db_from_factories.seconds else db_from_factories,
    )


@rule
def mocking(profile: Profile) -> Iterator[dict[str, Any]]:
    freezegun = profile.through("freezegun")
    if worth_it(freezegun.seconds, profile, 0.5, 0.01):
        yield insight(
            profile,
            "freezegun",
            "Replace freezegun with time-machine",
            category="mocking",
            impact=freezegun.seconds * 0.9,
            summary=f"freezegun takes {fmt(freezegun.seconds)}: it scans every loaded module each time it starts.",
            fix="time-machine (pip install time-machine) has the same decorator/context manager API "
            "(time_machine.travel) and mocks time at the C level, so starting it is nearly free.",
            safety="time-machine travels by default (the clock keeps running); use tick=False for a frozen clock.",
            docs="https://github.com/adamchainz/time-machine#migrating-from-freezegun",
            matched=freezegun,
        )
    autospec = profile.through("autospec")
    matched = profile.in_category("mocking")
    mock_seconds = matched.seconds - freezegun.by_category.get("mocking", 0.0)
    if worth_it(mock_seconds, profile, 1.0, 0.03):
        detail = f" ({fmt(autospec.seconds)} of it in autospec)" if autospec.seconds >= 0.2 else ""
        yield insight(
            profile,
            "mocking",
            "Mock setup is expensive",
            category="mocking",
            impact=mock_seconds * 0.5,
            summary=f"unittest.mock takes {fmt(mock_seconds)}{detail}.",
            fix="Patch once per class or module (a class-level decorator, or a module-scoped fixture with "
            "monkeypatch.context) instead of in every test or loop iteration; autospec=True/create_autospec "
            "introspects signatures each time, so reuse the spec'd mock.",
            matched=matched,
        )


@rule
def deepcopy(profile: Profile) -> Iterator[dict[str, Any]]:
    test_data = profile.through("testdata_copy")
    matched = profile.in_category("copying")
    if worth_it(test_data.seconds, profile, 0.3, 0.01):
        yield insight(
            profile,
            "django-testdata-deepcopy",
            "setUpTestData attributes are deep-copied on every access",
            category="copying",
            impact=test_data.seconds * 0.8,
            summary=f"Django deep-copies class attributes set in setUpTestData the first time each test reads them: "
            f"{fmt(test_data.seconds)} in total.",
            fix="Store only what tests need (model instances, not big dicts/lists or querysets of everything), keep "
            "large read-only data in module constants, or store ids and fetch in the test.",
            safety="The copy is what isolates tests from each other's in-memory changes: only move data that tests "
            "don't modify.",
            docs="https://docs.djangoproject.com/en/stable/topics/testing/tools/#django.test.TestCase.setUpTestData",
            matched=test_data,
        )
    elif worth_it(matched.seconds, profile, 1.0, 0.03):
        yield insight(
            profile,
            "deepcopy",
            "copy.deepcopy is expensive",
            category="copying",
            impact=matched.seconds * 0.5,
            summary=f"copy.deepcopy takes {fmt(matched.seconds)}.",
            fix="Deep-copy less: build fresh small objects per test, or share immutable data (tuples, frozen "
            "dataclasses, MappingProxyType) instead of copying it.",
            matched=matched,
        )


@rule
def imports_in_tests(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("imports")
    if worth_it(matched.seconds, profile, 1.0, 0.02):
        yield insight(
            profile,
            "imports-in-tests",
            "Modules are imported while tests run",
            category="imports",
            impact=matched.seconds * 0.3,
            summary=f"{fmt(matched.seconds)} of test time is spent importing modules: whichever test imports a heavy "
            "module first pays for it, which makes that test look slow (and varies with test order).",
            fix="Find what is imported at the lines listed. Import heavy modules at the top of the test module or in "
            "conftest.py so the cost shows up once at collection, or avoid importing them in tests at all "
            "(e.g. stub the heavy dependency).",
            matched=matched,
        )
    timeline = profile.doc["summary"].get("timeline", {})
    startup = timeline.get("startup", 0) + timeline.get("collection", 0)
    pytest = runner(profile) == "pytest"
    if startup >= 3 and startup >= profile.wall * 0.1:
        top = profile.doc["summary"].get("imports", {})
        modules = [f"{m['module']} ({fmt(m['seconds'])})" for m in top.get("collection", [])[:5]] + [
            f"{m['module']} ({fmt(m['seconds'])})" for m in top.get("startup", [])[:3]
        ]
        yield insight(
            profile,
            "slow-startup",
            "pytest takes a long time before the first test runs" if pytest else "Tests take a long time to start",
            category="imports",
            impact=startup * 0.4,
            summary=f"Startup takes {fmt(timeline.get('startup', 0))} and collection "
            f"{fmt(timeline.get('collection', 0))} on every run, even when you only run one test.",
            fix="Set testpaths and norecursedirs (node_modules, .venv, build, docs) so pytest doesn't walk unrelated "
            "directories; move heavy imports out of module level in test files and conftest.py into fixtures; "
            "check `python -X importtime -m pytest --collect-only -q` for the slowest imports."
            if pytest
            else "Move heavy imports out of module level in test modules (into setUpClass or the tests that need "
            "them), and run only the test packages you need; `python -X importtime` shows the slowest imports.",
            docs="https://docs.pytest.org/en/stable/reference/reference.html#confval-testpaths" if pytest else "",
            evidence=modules,
        )


GC_FREEZE_WHERE = {
    "pytest": "In conftest.py, at the end of pytest_sessionstart",
    "django": "In a DiscoverRunner subclass (TEST_RUNNER), at the end of setup_test_environment()",
    "unittest": "Before the tests run (in a load_tests hook, or your test script)",
}


@rule
def garbage_collection(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("gc")
    if not worth_it(matched.seconds, profile, 1.0, 0.03):
        return
    yield insight(
        profile,
        "gc",
        "The garbage collector is a noticeable share of test time",
        category="gc",
        impact=matched.seconds * 0.6,
        summary=f"Garbage collection pauses take {fmt(matched.seconds)} ({pct(matched.seconds, profile.test_time)}).",
        fix=f"{GC_FREEZE_WHERE[runner(profile)]}, call gc.freeze() after imports and setup so "
        "long-lived objects are never scanned again, and raise the first-generation threshold, e.g. "
        "gc.set_threshold(50_000, 50, 100).",
        safety="This trades memory for speed; watch memory use on large suites.",
        docs="https://docs.python.org/3/library/gc.html#gc.freeze",
        matched=matched,
    )


@rule
def logging_overhead(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("logging")
    if not worth_it(matched.seconds, profile, 1.0, 0.03):
        return
    yield insight(
        profile,
        "logging",
        "Logging is expensive in tests",
        category="logging",
        impact=matched.seconds * 0.6,
        summary=f"Formatting and emitting log records takes {fmt(matched.seconds)}.",
        fix="Raise the log level in test settings (e.g. WARNING), remove DEBUG-level handlers that format every "
        "record, and keep caplog to the tests that assert on logs.",
        safety="Tests that assert on log output need the logger enabled at that level (caplog.set_level).",
        matched=matched,
    )


@rule
def serialization(profile: Profile) -> Iterator[dict[str, Any]]:
    matched = profile.in_category("serialization")
    if not worth_it(matched.seconds, profile, 2.0, 0.05):
        return
    yaml = profile.through("yaml").seconds
    fix = "Parse fixture files once (functools.cache or a session-scoped fixture) instead of in every test."
    if yaml >= 0.5:
        fix += f" PyYAML takes {fmt(yaml)}: load with yaml.CSafeLoader (libyaml) instead of the pure-Python loader."
    yield insight(
        profile,
        "serialization",
        "Serialization is a large share of test time",
        category="serialization",
        impact=matched.seconds * 0.4,
        summary=f"JSON/YAML/pydantic (de)serialization takes {fmt(matched.seconds)}.",
        fix=fix,
        safety="Return copies of cached data if tests modify it.",
        matched=matched,
    )


@rule
def n_plus_one(profile: Profile) -> Iterator[dict[str, Any]]:
    offenders = []
    for test in profile.tests:
        db = test.get("db") or {}
        duplicates = [d for d in db.get("duplicates", []) if d["count"] >= 10 and d["sql"].upper().startswith("SELECT")]
        if duplicates:
            offenders.append((test, duplicates[0], db))
    if not offenders:
        return
    offenders.sort(key=lambda o: -o[1]["count"])
    impact = sum(db["time"] * (1 - 1 / max(dup["count"], 1)) for _, dup, db in offenders)
    sources = {dup.get("source", "django") for _, dup, _ in offenders}
    fixes = []
    if "django" in sources:
        fixes.append(
            "Django: fetch related rows in one query with select_related() for foreign keys and prefetch_related() "
            "for reverse/many-to-many relations, then lock the count in with assertNumQueries (or pytest-django's "
            "django_assert_num_queries)."
        )
    if "sqlalchemy" in sources:
        fixes.append(
            "SQLAlchemy: load relationships eagerly with selectinload()/joinedload() (or lazy='selectin'), or "
            "replace per-row queries with one grouped query (GROUP BY / a join), and assert the statement count "
            "in a test."
        )
    yield insight(
        profile,
        "n-plus-one",
        "Repeated queries (N+1) in the code under test",
        category="db",
        impact=impact,
        summary=f"{len(offenders)} tests run the same query 10+ times, e.g. {offenders[0][1]['count']}x in "
        f"{offenders[0][0]['name']}. N+1 queries slow the tests and production alike.",
        fix=" ".join(fixes),
        safety="This changes application code, so it speeds up production too; the tests stay as they are.",
        docs="https://docs.djangoproject.com/en/stable/topics/db/optimization/" if sources == {"django"} else "",
        tests=[test["nodeid"] for test, _, _ in offenders][:MAX_TESTS],
        evidence=[f"{dup['count']}x in {test['name']}: {dup['sql']}" for test, dup, _ in offenders[:5]],
    )


@rule
def database_server(profile: Profile) -> Iterator[dict[str, Any]]:
    engines = " ".join(django(profile).get("databases", []))
    connects = profile.through("db_connect")
    if worth_it(connects.seconds, profile, 0.5, 0.02):
        yield insight(
            profile,
            "db-connections",
            "Tests open new database connections over and over",
            category="db",
            impact=connects.seconds * 0.8,
            summary=f"Connecting to the database takes {fmt(connects.seconds)}.",
            fix="Reuse connections: create the engine once per session (not per test) and keep the default pool "
            "(NullPool reconnects on every checkout); in Django avoid closing connections in tests "
            "(connection.close()/close_old_connections).",
            matched=connects,
        )
    if not ("postgresql" in engines or "mysql" in engines):
        return
    # Durability costs only where the server flushes its log: at commits, and every write outside a transaction
    # commits on its own. TestCase rolls back, so a TestCase-only suite has next to nothing to gain.
    commits = commit_time = writes = write_time = 0.0
    measured = False
    committing: dict[str, float] = {}
    for test in profile.tests:
        db = test.get("db") or {}
        if "commits" in db:
            measured = True
            if spent := db["commit_time"] + db.get("autocommit_write_time", 0.0):
                committing[test["nodeid"]] = spent
            commits += db["commits"]
            commit_time += db["commit_time"]
            writes += db.get("autocommit_writes", 0)
            write_time += db.get("autocommit_write_time", 0.0)
    flush = profile.through("flush")
    if not measured:  # SQLAlchemy, or no query tracking: the sampled commits
        sampled = profile.through("commit")
        commit_time = sampled.seconds
    impact = commit_time + write_time + flush.seconds
    if not worth_it(impact, profile, 5.0, 0.02):
        return
    parts = []
    if commit_time:
        parts.append(f"{fmt(commit_time)} committing" + (f" ({commits:.0f} commits)" if measured else ""))
    if write_time:
        parts.append(f"{fmt(write_time)} in {writes:.0f} writes outside a transaction (each commits)")
    if flush.seconds:
        parts.append(f"{fmt(flush.seconds)} flushing tables after TransactionTestCase tests")
    yield insight(
        profile,
        "db-server-durability",
        "Make the test database server skip durability work",
        category="db",
        impact=impact,
        summary=f"Tests spend {', '.join(parts)}: that's where the server waits for its log to reach the disk. "
        "Test databases don't need to survive a crash. This is the most it can save: the round trips stay.",
        fix="For a PostgreSQL test server (Docker/CI service) start it with -c fsync=off -c synchronous_commit=off "
        "-c full_page_writes=off, and put its data directory on tmpfs; for MySQL use "
        "--innodb-flush-log-at-trx-commit=0 --skip-log-bin. Never on a server with data you need.",
        safety="Only affects crash safety of the throwaway test server; query behaviour is identical.",
        docs="https://www.postgresql.org/docs/current/non-durability.html",
        tests=[nodeid for nodeid, _ in top(committing, MAX_TESTS)] or flush.tests(),
    )


@rule
def parallelism(profile: Profile) -> Iterator[dict[str, Any]]:
    run = profile.doc["run"]
    cpus = run.get("platform", {}).get("cpu_count") or 1
    workers = run.get("workers") or 1
    if not profile.tests:
        return
    slowest = max(test["duration"] for test in profile.tests)
    pytest = runner(profile) == "pytest"
    if workers > 1:
        per_worker: defaultdict[str, float] = defaultdict(float)
        for test in profile.tests:
            per_worker[test.get("worker") or "main"] += test["duration"]
        if len(per_worker) > 1:
            busiest, least = max(per_worker.values()), min(per_worker.values())
            if busiest - least >= 5 and busiest >= least * 1.3:
                yield insight(
                    profile,
                    "xdist-balance",
                    ("pytest-xdist workers" if pytest else "Workers") + " finish at very different times",
                    category="parallelism",
                    impact=(busiest - least) / 2,
                    summary=f"The busiest worker ran {fmt(busiest)} of tests, the least busy {fmt(least)}.",
                    fix="Use --dist worksteal (pytest-xdist 3.2+) so idle workers take tests from busy ones; if you "
                    "use --dist loadscope/loadfile, split the biggest classes or modules."
                    if pytest
                    else "Django gives each worker whole test classes, so one slow class keeps a worker busy: split "
                    "the slowest classes.",
                    docs="https://pytest-xdist.readthedocs.io/en/stable/distribution.html" if pytest else "",
                )
        return
    if cpus < 2 or profile.test_time < 20:
        return
    estimate = max(profile.test_time / cpus, slowest)
    saved = profile.test_time - estimate
    if saved < 10:
        return
    context = profile.context.get("pytest") or {}
    install = "" if context.get("xdist_installed") else "pip install pytest-xdist, then "
    if runner(profile) == "django":
        with_what = "With manage.py test --parallel"
        fix = (
            "Run manage.py test --parallel auto (or only in CI). Django gives each worker its own copy of the test "
            "database."
        )
        order = "Run with --shuffle first to shake out order dependencies."
        docs = "https://docs.djangoproject.com/en/stable/ref/django-admin/#cmdoption-test-parallel"
    elif runner(profile) == "unittest":
        with_what = "With pytest-xdist"
        fix = (
            "pytest runs unittest test cases as they are: pip install pytest pytest-xdist, then "
            "flakytest pytest -n auto."
        )
        order = "Run with -p randomly first to shake out order dependencies."
        docs = "https://docs.pytest.org/en/stable/how-to/unittest.html"
    else:
        with_what = "With pytest-xdist"
        fix = (
            f"{install}run pytest -n auto (add it to addopts, or only in CI). pytest-django creates a separate "
            "test database per worker automatically."
        )
        order = "Run with -p randomly first to shake out order dependencies."
        docs = "https://pytest-xdist.readthedocs.io/en/stable/"
    yield insight(
        profile,
        "xdist",
        f"Run tests in parallel on all {cpus} CPUs",
        category="parallelism",
        impact=saved,
        summary=f"Tests run one at a time: {fmt(profile.test_time)} of test time on a {cpus}-CPU machine. "
        f"{with_what} this could take about {fmt(estimate)}.",
        fix=fix,
        safety="Tests must not share state: files in fixed paths, ports, a shared database without per-worker names, "
        f"or module globals. {order}",
        docs=docs,
    )


@rule
def coverage(profile: Profile) -> Iterator[dict[str, Any]]:
    pytest = profile.context.get("pytest") or {}
    python = tuple(profile.context.get("python_version") or (0, 0))
    if pytest.get("cov") and (3, 12) <= python < (3, 14) and not pytest.get("coverage_core"):
        yield insight(
            profile,
            "coverage-sysmon",
            "Measure coverage with sys.monitoring",
            category="coverage",
            impact=profile.test_time * 0.1,
            summary="pytest-cov is active with the default tracer. On Python 3.12+ coverage.py can use sys.monitoring, "
            "which has much lower overhead.",
            fix="Set COVERAGE_CORE=sysmon in the environment (coverage 7.4+). Branch coverage needs Python 3.14 "
            "for sysmon.",
            safety="Coverage numbers are the same; only how they're measured changes.",
            docs="https://coverage.readthedocs.io/en/latest/cmd.html#execution-measurement",
        )


@rule
def django_settings(profile: Profile) -> Iterator[dict[str, Any]]:
    settings = django(profile)
    if not settings:
        return
    toolbar = profile.through("debug_toolbar")
    if settings.get("debug_toolbar") and toolbar.seconds >= 0.2:
        yield insight(
            profile,
            "django-debug-toolbar",
            "Disable django-debug-toolbar in tests",
            category="app_code",
            impact=toolbar.seconds,
            summary=f"django-debug-toolbar is installed in the test settings and costs {fmt(toolbar.seconds)}.",
            fix="Only add debug_toolbar to INSTALLED_APPS and MIDDLEWARE when not testing "
            "(e.g. `if DEBUG and not TESTING:`).",
            docs="https://django-debug-toolbar.readthedocs.io/en/latest/installation.html#disable-the-toolbar-when-running-tests-optional",
            matched=toolbar,
        )
    # DEBUG's own cost is the bookkeeping around each query, not the query. With DEBUG off, only
    # assertNumQueries / CaptureQueriesContext use the debug cursor, and they need it.
    debug_sql = profile.through("debug_sql")
    if settings.get("debug") and worth_it(debug_sql.seconds, profile, 0.5, 0.01):
        yield insight(
            profile,
            "django-debug-in-tests",
            "Tests run with DEBUG = True",
            category="db",
            impact=debug_sql.seconds,
            summary=f"DEBUG is on while tests run, so Django times and records every SQL query in "
            f"connection.queries: {fmt(debug_sql.seconds)} of bookkeeping.",
            fix="Don't force DEBUG = True in tests (Django's test runner sets it to False unless you pass "
            "--debug-mode, pytest-django unless you set django_debug_mode).",
            safety="Tests that rely on DEBUG behaviour (e.g. error pages) can override it with override_settings.",
            matched=debug_sql,
        )
    static = profile.through("static_files")
    if worth_it(static.seconds, profile, 0.5, 0.01):
        yield insight(
            profile,
            "django-static-storage",
            "Use plain static file storage in tests",
            category="app_code",
            impact=static.seconds,
            summary=f"Static file storage, finders and WhiteNoise take {fmt(static.seconds)} of their own work "
            "(hashing names, reading the manifest, looking up files).",
            fix='In test settings use STORAGES["staticfiles"] = {"BACKEND": '
            '"django.contrib.staticfiles.storage.StaticFilesStorage"} so templates don\'t need a manifest, and '
            "WHITENOISE_AUTOREFRESH = False.",
            matched=static,
        )
    celery = profile.through("celery")
    if celery.seconds >= 0.5 and not settings.get("celery_eager"):
        yield insight(
            profile,
            "celery-eager",
            "Run Celery tasks eagerly in tests",
            category="app_code",
            impact=celery.seconds * 0.7,
            summary=f"Celery machinery takes {fmt(celery.seconds)}.",
            fix="Set CELERY_TASK_ALWAYS_EAGER = True in test settings (or call task functions directly) so tasks "
            "run in-process without a broker.",
            safety="Eager tasks skip serialization; keep a few tests that run a real worker if you depend on it.",
            docs="https://docs.celeryq.dev/en/stable/userguide/testing.html",
            matched=celery,
        )


@rule
def async_loops(profile: Profile) -> Iterator[dict[str, Any]]:
    loops = profile.through("event_loops")
    if not worth_it(loops.seconds, profile, 0.5, 0.02):
        return
    pytest = runner(profile) == "pytest"
    yield insight(
        profile,
        "asyncio-loop-per-test",
        "A new event loop is created for every async test",
        category="pytest",
        impact=loops.seconds * 0.8,
        summary=f"Creating and closing event loops takes {fmt(loops.seconds)}.",
        fix="With pytest-asyncio 0.24+ set asyncio_default_fixture_loop_scope / loop_scope='session' (or "
        "@pytest.mark.asyncio(loop_scope='session')) so tests share one loop."
        if pytest
        else "IsolatedAsyncioTestCase creates a new event loop for every test, by design: make the loop cheaper to "
        "create (no PYTHONASYNCIODEBUG, a lighter loop_factory), or write these as pytest-asyncio tests that share "
        "one loop per session.",
        safety="Tests then share loop state: don't leave tasks running between tests.",
        docs="https://pytest-asyncio.readthedocs.io/en/stable/how-to-guides/run_session_tests_in_same_loop.html"
        if pytest
        else "https://docs.python.org/3/library/unittest.html#unittest.IsolatedAsyncioTestCase",
        matched=loops,
    )


@rule
def slowest_tests(profile: Profile) -> Iterator[dict[str, Any]]:
    if not profile.test_time:
        return
    for test in sorted(profile.tests, key=own_time, reverse=True)[:3]:
        own = own_time(test)
        if own < 2 or own < profile.test_time * 0.1:
            continue
        top = max(test["categories"].items(), key=lambda kv: kv[1], default=("unknown", 0))
        segment = test["segments"][0]["label"] if test["segments"] else "unknown"
        yield insight(
            profile,
            f"slow-test-{test['key']}",
            f"{test['name']} alone takes {pct(test['duration'], profile.test_time)} of test time",
            category=top[0],
            impact=test["duration"] * 0.5,
            summary=f"{test['nodeid']} takes {fmt(test['duration'])}: mostly {top[0].replace('_', ' ')} "
            f"({fmt(top[1])}), biggest piece: {segment}.",
            fix=f"Open its breakdown (flakytest report --test '{test['nodeid']}') and fix the biggest piece first. "
            "If it's an integration test that has to be slow, mark it (@pytest.mark.slow) and run it in a "
            "separate CI job.",
            tests=[test["nodeid"]],
        )


def own_time(test: dict[str, Any]) -> float:
    """A test's duration without what other tests share (class/module/session fixtures) and one-off warm-up."""
    return test["duration"] - test.get("shared_fixture_time", 0) - (test.get("warmup") or {}).get("seconds", 0)


def generate(
    doc: dict[str, Any], context: dict[str, Any], matched: dict[str, Matched] | None = None, rootdir: Path | None = None
) -> list[dict[str, Any]]:
    profile = Profile(doc, context, matched, rootdir)
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for check in RULES:
        for item in check(profile):
            if item["id"] not in seen:
                seen.add(item["id"])
                found.append(item)
    found.sort(key=lambda item: -item["impact"])
    return found
