# The flakytest profile format (v1)

`flakytest pytest` (and `flakytest python` for unittest and Django's test runner) writes one JSON document per
test session to
`.flakytest/runs/<run id>/profile.json`. The same document is what the local report renders, what
`flakytest report` summarises for people and agents, and what is uploaded to flakytest.dev
(`POST /api/v1/profile`, gzip-compressed, `Authorization: <FLAKYTEST_SECRET_TOKEN>`).

Next to it, `profile.js` is the same document as a script (`window.FLAKYTEST_PROFILE = {…};`) that `index.html`
loads, so the report works straight from disk, and `summary.json` has just each test's node id, duration and
outcome (and the run's id and duration), which is all the next run needs to compare itself with this one.

Consumers must ignore keys they don't know. Times are seconds (floats), timestamps are ISO 8601 UTC.

## Uploading in parts

A profile is uploaded in parts so no request (and no worker on flakytest.dev) has to hold a whole large suite.
Each part is a valid profile document with the `run` and `summary`, up to 2,000 tests (fewer if the part would
be larger than 16 MB of JSON), and only the `frames`, `stacks`, `stack_categories` and `sources` those tests use,
renumbered. The first part also carries `insights`, `warnings`, `fixtures` and `suite_stacks`. Every part has

```jsonc
"part": {"index": 0, "count": 18}   // 0-based; a document without "part" is the only part
```

Parts of one profile share `run.id`. Uploads are idempotent: the same part again changes nothing, and a changed
part replaces the old one, so retries and re-uploads are safe. flakytest.dev processes the parts in parallel and
counts the profile in its trends once all parts are in.

## How samples are attributed to tests

The plugin runs every test phase (`setup`, `call`, `teardown`) inside a tiny generated function whose
code object is named `__flakytest_<phase>_<key>__` with the filename `<flakytest>`, where `<key>` is the
first 12 hex characters of `sha1(nodeid)`. A sampling profiler (tachyon on Python 3.15+, py-spy before
that) captures that frame in every stack it samples while the phase runs, so attribution needs no clock
synchronisation and works the same for both profilers and for pytest-xdist workers. Samples without a
marker frame are classified by the pytest hooks on their stack (`startup`, `collection`,
`session_teardown`, `other`).

Under `flakytest python`, unittest's `TestCase.run` (or Django's `SimpleTestCase._setup_and_call`) is the test,
and its phases are `setup` (Django's `_pre_setup`, `setUp`), `call` (the test method) and `teardown` (`tearDown`,
cleanups, Django's `_post_teardown`, and the previous class's or module's teardown). Node ids look like pytest's:
`tests/test_orders.py::TestCheckout::test_list`. `setUpModule` and `setUpClass` (which runs Django's
`setUpTestData`) are module and class fixtures of those names, timed in the setup of the first test that needs
them, and Django's test databases are the session fixture `django_db_setup`. Under `manage.py test --parallel`
each worker (`django-1`, `django-2`, …) profiles its tests like a pytest-xdist worker.

The pytest durations are exact; sample counts only decide how a phase's duration is split. A stack's
estimated time is `count / samples_in_phase * phase_duration`. Counts are weighted: tachyon samples a test at a
quarter of the rate once it has 200 samples, and big suites (over 10,000 tests) at 500 Hz unless
`--flakytest-rate` says otherwise; each sample then counts as the samples it stands for at the run's `rate`.

With tachyon, other threads are sampled while the main thread isn't running Python (it waits on a thread pool, a
lock or I/O), and tied to the test phase the main thread is in at that instant. When the main thread waited while
other threads worked, the waiting time is split between what they did (`threads`, `thread_stacks`); threads that
wait too, or only run the standard library and pytest's plumbing, don't count. py-spy aggregates samples per
stack, so with it other threads are left out.

Every test process builds its part of the document when its session ends (under pytest-xdist the workers do that
at the same time) and the controller merges them. A worker that crashes leaves no part: the controller builds it
from the records the worker wrote as each test finished and the samples its profiler wrote, and takes any test it
didn't record from pytest's reports (exact duration and outcome only). The `missing_workers` warning says so.

## Document

```jsonc
{
  "format": "flakytest-profile",
  "version": 1,
  "run": {
    "id": "8c3f…",                       // uuid4 hex, unique per session
    "started_at": "2026-10-16T21:03:11Z",
    "duration": 84.2,                    // wall time of the pytest session
    "exit_status": "OK",                 // pytest ExitCode name: OK, TESTS_FAILED, INTERRUPTED, …
    "argv": ["tests/", "-x"],
    "runner": "pytest",                  // "pytest", "unittest" or "django" (manage.py test)
    "flakytest_version": "2026.0b1",
    "python": {"version": "3.15.0", "implementation": "CPython"},
    "platform": {"system": "Linux", "release": "6.8.0", "machine": "x86_64", "cpu_count": 8},
    "environment": {
      "kind": "ci",                      // "ci" or "local"
      "name": "ci:github-actions:test",  // stable grouping key, override with FLAKYTEST_ENV
      "provider": "github-actions",      // github-actions, gitlab, circleci, buildkite, jenkins, ci, local
      "details": {"runner_os": "Linux", "runner_arch": "X64", "job": "test", "workflow": "CI"}
    },
    "git": {
      "commit_hash": "…", "tree_hash": "…", "branch": "main", "subject": "…",
      "dirty": true,
      "changed_files": ["src/app/models.py"]   // files changed by HEAD + uncommitted changes
    },
    "profiler": {
      "backend": "tachyon",              // "tachyon", "py-spy" or "none"
      "rate": 1000,                      // requested samples per second
      "samples": 81234,                  // samples captured (all processes)
      "error": null                      // why profiling was unavailable, if backend == "none"
    },
    "workers": 1,                        // pytest-xdist or Django --parallel workers (1 without them)
    "plugins": ["django", "xdist", "cov"],
    // building the profile after the last test (seconds into the session it finished): the slowest process's
    // part (they're built at the same time), then merging them on the controller, writing the files comes on top
    "postprocessing": {"tests_finished_at": 82.1, "parts": 1.2, "merge": 0.4}
  },

  "summary": {
    "tests": 812, "passed": 800, "failed": 2, "skipped": 10, "errors": 0,
    "test_time": 301.5,                  // sum of all test durations (setup + call + teardown)
    "phases": {"setup": 120.1, "call": 160.0, "teardown": 21.4},
    "categories": {"db": 140.2, "password_hashing": 40.1},   // estimated seconds, see categories below
    "timeline": {"startup": 2.1, "collection": 3.4, "tests": 77.9, "between_tests": 0.3, "session_teardown": 0.8},
    // the modules being imported while pytest starts and collects
    "imports": {"collection": [{"module": "tests/test_api.py", "seconds": 1.2}]},
    // the test processes' memory (bytes, see Memory below); null when it couldn't be measured
    "memory": {
      "source": "linux",                 // "linux", "psutil", "maxrss" (how it was measured)
      "peak_per_test": true,             // whether every test has its own peak (Linux only)
      "private": true,                   // whether the added-up values are private memory (Linux only)
      "peak": 734003200,                 // the highest peak RSS of any one process
      "machine_peak": 2516582400,        // all processes added up: the most the run needed at once
      "machine_peak_at": 41.2,           // seconds into the run
      "machine_peak_tests": ["…"],       // the tests running at that moment, at most 20
      "processes": [{"worker": "gw0", "tests": 204, "peak": 734003200, "start": 98566144, "end": 312475648,
                     "growth": 213909504}],
      "timeline": [[0.8, 412090368], …]  // [seconds into the run, all processes added up], at most 120 points
    }
  },

  // Interned frames: [path, function, line, kind]. Paths inside the project are relative to the
  // rootdir, installed packages start at the package (django/db/models/query.py), the standard
  // library starts with "<stdlib>/" (<stdlib>/json/encoder.py).
  // kind: "test" | "app" | "lib" | "stdlib" | "framework" (pytest, pluggy, unittest) | "native" (<GC>, <native>)
  "frames": [["tests/test_orders.py", "test_checkout", 42, "test"], …],

  // Interned stacks as a prefix tree: each node is [parent node index or -1, frame index]. A stack is a
  // node; walk the parents to get its frames from the outermost frame below the marker to the leaf.
  "stacks": [[-1, 3], [0, 7], [1, 12], …],
  // The category of every node a test's samples end in (see categories below)
  "stack_categories": {"2": "db", …},
  // Source lines of the project frames that the report points at (segments, hot lines)
  "sources": {"0": "response = self.client.post(url, data)", …},

  "tests": [
    {
      "nodeid": "tests/test_orders.py::TestCheckout::test_checkout[eur]",
      "key": "1f3a9c0d2b4e",             // sha1(nodeid)[:12], the marker key
      "file": "tests/test_orders.py",
      "class": "TestCheckout",           // null for module-level tests
      "name": "test_checkout[eur]",
      "outcome": "passed",               // passed, failed, error, skipped, xfailed, xpassed
      "worker": null,                    // "gw0" … under pytest-xdist, "django-1" … under manage.py test --parallel
      "duration": 1.82,
      "phases": {"setup": 1.2, "call": 0.6, "teardown": 0.02},
      "samples": {"setup": 1190, "call": 602, "teardown": 21},
      // per phase, [stack index, sample count]. Empty for tests faster than 100 ms outside the 2,000 slowest of
      // their process: their samples are folded into "suite_stacks" to keep the document small
      "stacks": {"setup": [[3, 900], [4, 290]], "call": […], "teardown": […]},
      // exact setup times of the fixtures this test executed (cached ones are omitted) that took 5 ms or more, or
      // have a wider scope than function; "fixtures" below has every call. A class/module/session fixture's
      // queries and the private memory it kept are its own, not the test's
      "fixtures": [{"name": "db", "scope": "function", "duration": 0.31},
                   {"name": "django_db_setup", "scope": "session", "duration": 9.1,
                    "db": {"queries": 412, "time": 2.1, "duplicates": […]}, "memory": 31457280}],
      // setup time of class/module/session fixtures this test happened to trigger first
      "shared_fixture_time": 0.0,
      // estimated seconds per category, see below; they add up to "duration"
      "categories": {"db": 1.1, "password_hashing": 0.4, "test_code": 0.1},
      // the top-level pieces of work in each phase: fixtures, setUp, the test body, …
      "segments": [{"phase": "setup", "label": "fixture django_db_setup", "frame": 12, "seconds": 0.9}],
      // the deepest frame in project code (kind test/app) for each sample: "which line of mine is slow"
      "hot_lines": [{"frame": 0, "seconds": 0.5, "categories": {"db": 0.45}}],
      // database instrumentation (Django execute_wrapper / SQLAlchemy events), when available; with Django also
      // commits and writes outside a transaction (each one commits on its own): what durability costs
      "db": {"queries": 31, "time": 0.42,
             "duplicates": [{"sql": "SELECT … FROM \"orders_item\" WHERE \"order_id\" = %s", "count": 12}],
             "commits": 0, "commit_time": 0.0, "autocommit_writes": 2, "autocommit_write_time": 0.004},
      // other threads that worked while this test's main thread waited (tachyon only): how many, and what they
      // did, which the categories above count instead of the waiting
      "threads": {"count": 4, "seconds": 6.1, "categories": {"network": 3.7, "serialization": 1.8}},
      // per phase, [stack index, estimated seconds] of those threads' samples
      "thread_stacks": {"call": [[41, 2.2], …]},
      // one-off costs this test paid for whichever test runs first: a fixture's first call in its process that was
      // much slower than its other calls (over 0.5 s and 10x the median), and imports
      "warmup": {"seconds": 4.6, "causes": [{"what": "first mock_aws_services call in gw2", "seconds": 4.6}]},
      // the test process's resident memory (bytes): RSS when the test started and ended, the peak while it ran,
      // and private memory (RssAnon on Linux); keys that couldn't be measured are left out, see Memory below
      "memory": {"at": 12.31, "start": 104857600, "end": 106954752, "peak": 524288000,
                 "peak_increase": 419430400, "growth": 2097152,
                 "private_start": 89128960, "private_end": 91226112,
                 "warmup": "first test in its process"},   // or "first test in its class"; absent otherwise
      "longrepr": null,                  // failure text (trimmed), for failed tests
      "warnings": ["undersampled"]       // see warnings below; "worker_crashed" / "not_recorded" for tests
                                         // known only from pytest's reports
    }
  ],

  // the folded tests' samples (see "stacks" above) as one tree: [stack index, category, estimated seconds];
  // nodes with less than 0.02% of its time are merged into their parent
  "suite_stacks": [[7, "db", 12.5], …],

  // aggregated exact fixture timings across the session
  // median and p99 come from a histogram (within ~12%); over_1s counts calls that took more than a second
  "fixtures": [{"name": "django_db_setup", "scope": "session", "calls": 1, "total": 9.1, "max": 9.1, "mean": 9.1,
                "median": 9.1, "p99": 9.1, "over_1s": 1, "tests": 1}],

  // "fix next" suggestions, sorted by estimated impact
  "insights": [
    {
      "id": "django-password-hasher",
      "title": "Use a fast password hasher in tests",
      "category": "password_hashing",
      "severity": "high",                // high, medium, low
      "impact": 40.1,                    // estimated seconds per run this costs
      "summary": "40.1s (13%) of test time is spent hashing passwords with PBKDF2.",
      "fix": "Add PASSWORD_HASHERS = [\"django.contrib.auth.hashers.MD5PasswordHasher\"] to your test settings.",
      "safety": "Hash strength is irrelevant in tests; only use MD5 in test settings.",
      "docs": "https://docs.djangoproject.com/en/stable/topics/testing/overview/#password-hashing",
      "tests": ["tests/test_auth.py::test_login", …],   // most affected tests, at most 20
      // project lines that trigger it, at most 10
      "locations": [{"frame": 4, "location": "tests/factories.py:12", "function": "user", "seconds": 12.5}],
      "evidence": []                                     // extra facts, e.g. the repeated SQL
    }
  ],

  "warnings": [
    {"code": "undersampled", "message": "14 tests got no samples …", "tests": ["…"], "recommended_rate": 5000}
  ]
}
```

## Categories

Every sample below a marker is assigned exactly one category from its frames:

1. The garbage collector wins outright: it's always the leaf frame (`<GC>`).
2. A `sleep(…)` call on the sampled line is `sleep`.
3. **Context** categories hold for everything below them, whatever it does: the one closest to the root wins
   (an import that queries the database is import time).
4. Otherwise the **deepest** resource or code category wins (JSON parsing inside an HTTP client is serialization).
   Network or waiting below a database or cache client belongs to that client.
5. Otherwise the leaf frame decides: `test_code`, `app_code`, `pytest` or `library`.

| category            | tier     | matches                                                                  |
|---------------------|----------|--------------------------------------------------------------------------|
| `gc`                | leaf     | the garbage collector (`<GC>` frames)                                    |
| `imports`           | context  | importlib                                                                |
| `password_hashing`  | context  | django.contrib.auth.hashers, bcrypt, argon2, passlib, hashlib.pbkdf2     |
| `migrations`        | context  | django.db.migrations, alembic                                            |
| `db_schema`         | context  | Django flush / create test db, SQLAlchemy `create_all` / `drop_all`      |
| `fixture_loading`   | context  | Django loaddata / serializers                                            |
| `browser`           | context  | selenium, playwright                                                     |
| `subprocess`        | context  | subprocess, multiprocessing, testcontainers, docker                      |
| `db`                | deepest  | django.db, psycopg, psycopg2, sqlite3, sqlalchemy, asyncpg, pymysql, …   |
| `cache`             | deepest  | redis, memcache, Django cache backends                                   |
| `network`           | deepest  | socket and ssl I/O only (`recv`, `send`, `connect`, `getaddrinfo`, TLS handshakes); an HTTP client's own work (building requests, parsing responses) is whatever it does |
| `sleep`             | deepest  | `time.sleep(…)` on the sampled line, `asyncio.sleep`                     |
| `waiting`           | deepest  | `Event.wait`, `Thread.join`, `Queue.get`, `select()`, `Future.result`    |
| `template`          | deepest  | django.template, jinja2, mako                                            |
| `factories`         | deepest  | factory_boy, model_bakery, faker, mixer, hypothesis                      |
| `mocking`           | deepest  | unittest.mock, pytest-mock, freezegun, time-machine, responses           |
| `copying`           | deepest  | copy.deepcopy (Django `setUpTestData` copies class attributes per test)  |
| `serialization`     | deepest  | json, pickle, pydantic, marshmallow, DRF serializers, yaml               |
| `logging`           | deepest  | logging                                                                  |
| `test_code`         | fallback | leaf frame in a test file                                                |
| `app_code`          | fallback | leaf frame in project code                                               |
| `pytest`            | fallback | leaf frame in pytest, pluggy, unittest or a wrapper around the test      |
| `library`           | fallback | anything else                                                            |
| `unsampled`         |          | a phase that got no samples (its measured duration)                      |

## Warnings

* `undersampled` — tests that should have been sampled (at least three sample intervals long) got no
  samples, or tests with no or fewer than 5 samples add up to 5% of test time. `recommended_rate` is the
  rate that would give the median of those tests 10 samples. Rerun with `--flakytest-rate <rate>`.
* `short_tests` — tests shorter than a sample interval got no samples. Expected, informational.
* `profiler_unavailable` — no sampling profiler could attach; durations, fixtures and query counts are
  still exact, but there are no stacks.
* `sample_errors` — the profiler failed to read a large share of samples.
* `missing_workers` — pytest-xdist workers crashed (`workers` lists them). Their recorded tests are in the
  profile; tests they didn't record have only exact durations and outcomes.
* `samples_incomplete` — a profiler didn't finish writing its samples (its process was killed), so the last tests'
  breakdowns are missing.
* `memory_growth` — a test process's memory grew by at least 256 MB (and half of what it started with) over the
  run without being freed. `tests` lists the tests it grew most in, `growth` is the growth in bytes.

## Memory

Every test process measures its own memory around each test, so under pytest-xdist each worker measures the tests
it runs. A test's window starts before its setup and ends when the next test starts, so what pytest frees after a
test isn't counted as kept. `peak_increase` is how much memory the test needed above what it started with;
`growth` is how much it kept.

* **Linux**: the kernel's peak counter (`VmHWM`) is reset before every test by writing `5` to
  `/proc/self/clear_refs`, so every test gets its own peak. What a test needed and kept is private memory
  (`RssAnon`): resident memory also counts file pages (the interpreter, libraries, mapped files) that the kernel
  drops and reads back as it likes, especially in a memory-limited container. The peak counter only exists for
  resident memory as a whole, so `peak_increase` is the peak less the larger of the file-backed memory the test
  started and ended with, less the private memory it started with. Private memory leaves out what every process
  maps, so processes can be added up for `machine_peak`.
* Class, module and session fixtures restart a test's window when they finish: the private memory they keep is
  recorded on the fixture (`fixtures[].memory`), not charged to the test that happened to run them first. The first
  test in each process and class is flagged (`memory.warmup`); reports leave them, and tests that set up a shared
  fixture, out of the tests that needed the most memory.
* **macOS and Windows**: the peak can't be reset, so `peak` is only known for tests that raised their process's
  peak. With [psutil](https://pypi.org/project/psutil/) installed `start`, `end` and `growth` are known for
  every test; without it only the raised peaks are (macOS), or nothing (Windows). The values added up across
  workers are RSS, which counts shared memory once per process.
* `machine_peak` assumes each worker was at its test's peak for the whole test, so it is an upper bound, and
  the xdist controller process isn't included.
* Only the test processes are measured: memory used by a database server, a browser or subprocesses isn't.
* `growth` is noisy: Python and the C allocator often keep freed memory for reuse, so a test can show growth that
  later tests reuse. Peaks are reliable.
