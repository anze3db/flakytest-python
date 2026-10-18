# The flakytest profile format (v1)

`pytest --flakytest-profile` (or `flakytest run`) writes one JSON document per test session to
`.flakytest/runs/<run id>/profile.json`. The same document is what the local report renders, what
`flakytest report` summarises for people and agents, and what is uploaded to flakytest.dev
(`POST /api/v1/profile`, gzip-compressed, `Authorization: <FLAKYTEST_SECRET_TOKEN>`).

Consumers must ignore keys they don't know. Times are seconds (floats), timestamps are ISO 8601 UTC.

## Uploading in parts

A profile is uploaded in parts so no request (and no worker on flakytest.dev) has to hold a whole large suite.
Each part is a valid profile document with the `run` and `summary`, up to 2,000 tests (fewer if the part would
be larger than 16 MB of JSON), and only the `frames`, `stacks`, `stack_categories` and `sources` those tests use,
renumbered. The first part also carries `insights`, `warnings` and `fixtures`. Every part has

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

The pytest durations are exact; sample counts only decide how a phase's duration is split. A stack's
estimated time is `count / samples_in_phase * phase_duration`.

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
    "workers": 1,                        // pytest-xdist workers (1 without xdist)
    "plugins": ["django", "xdist", "cov"]
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
      "worker": null,                    // "gw0" … under pytest-xdist
      "duration": 1.82,
      "phases": {"setup": 1.2, "call": 0.6, "teardown": 0.02},
      "samples": {"setup": 1190, "call": 602, "teardown": 21},
      // per phase, [stack index, sample count]
      "stacks": {"setup": [[3, 900], [4, 290]], "call": […], "teardown": […]},
      // exact fixture setup times for fixtures this test executed (cached ones are omitted)
      "fixtures": [{"name": "db", "scope": "function", "duration": 0.31}],
      // setup time of class/module/session fixtures this test happened to trigger first
      "shared_fixture_time": 0.0,
      // estimated seconds per category, see below; they add up to "duration"
      "categories": {"db": 1.1, "password_hashing": 0.4, "test_code": 0.1},
      // the top-level pieces of work in each phase: fixtures, setUp, the test body, …
      "segments": [{"phase": "setup", "label": "fixture django_db_setup", "frame": 12, "seconds": 0.9}],
      // the deepest frame in project code (kind test/app) for each sample: "which line of mine is slow"
      "hot_lines": [{"frame": 0, "seconds": 0.5, "categories": {"db": 0.45}}],
      // database instrumentation (Django execute_wrapper / SQLAlchemy events), when available
      "db": {"queries": 31, "time": 0.42,
             "duplicates": [{"sql": "SELECT … FROM \"orders_item\" WHERE \"order_id\" = %s", "count": 12}]},
      // the test process's resident memory (bytes): RSS when the test started and ended, the peak while it ran,
      // and private memory (RssAnon on Linux); keys that couldn't be measured are left out, see Memory below
      "memory": {"at": 12.31, "start": 104857600, "end": 106954752, "peak": 524288000,
                 "peak_increase": 419430400, "growth": 2097152,
                 "private_start": 89128960, "private_end": 91226112},
      "longrepr": null,                  // failure text (trimmed), for failed tests
      "warnings": ["undersampled"]       // see warnings below
    }
  ],

  // aggregated exact fixture timings across the session
  "fixtures": [{"name": "django_db_setup", "scope": "session", "calls": 1, "total": 9.1, "max": 9.1, "tests": 1}],

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

Every sample below a marker is assigned exactly one category by the first rule (in this order) that
matches any frame of its stack:

| category            | matches                                                                  |
|---------------------|--------------------------------------------------------------------------|
| `gc`                | the garbage collector (`<GC>` frames)                                    |
| `imports`           | importlib                                                                |
| `password_hashing`  | django.contrib.auth.hashers, bcrypt, argon2, passlib, hashlib.pbkdf2     |
| `migrations`        | django.db.migrations, alembic                                            |
| `db_schema`         | Django flush / create test db, SQLAlchemy `create_all` / `drop_all`      |
| `fixture_loading`   | Django loaddata / serializers                                            |
| `browser`           | selenium, playwright                                                     |
| `subprocess`        | subprocess, multiprocessing, testcontainers, docker                      |
| `db`                | django.db, psycopg, psycopg2, sqlite3, sqlalchemy, asyncpg, pymysql, …   |
| `cache`             | redis, memcache, Django cache backends                                   |
| `network`           | socket, ssl, http.client, urllib3, requests, httpx, aiohttp, botocore    |
| `sleep`             | `time.sleep(…)` on the sampled line, `asyncio.sleep`                     |
| `waiting`           | `Event.wait`, `Thread.join`, `Queue.get`, `select()`                     |
| `template`          | django.template, jinja2, mako                                            |
| `factories`         | factory_boy, model_bakery, faker, mixer, hypothesis                      |
| `mocking`           | unittest.mock, pytest-mock, freezegun, time-machine, responses           |
| `copying`           | copy.deepcopy (Django `setUpTestData` copies class attributes per test)  |
| `serialization`     | json, pickle, pydantic, marshmallow, DRF serializers, yaml               |
| `logging`           | logging                                                                  |
| `test_code`         | leaf frame in a test file                                                |
| `app_code`          | leaf frame in project code                                               |
| `pytest`            | leaf frame in pytest, pluggy, unittest or a wrapper around the test      |
| `library`           | anything else                                                            |
| `unsampled`         | a phase that got no samples (its measured duration)                      |

## Warnings

* `undersampled` — tests that should have been sampled (at least three sample intervals long) got no
  samples, or tests with no or fewer than 5 samples add up to 5% of test time. `recommended_rate` is the
  rate that would give the median of those tests 10 samples. Rerun with `--flakytest-rate <rate>`.
* `short_tests` — tests shorter than a sample interval got no samples. Expected, informational.
* `profiler_unavailable` — no sampling profiler could attach; durations, fixtures and query counts are
  still exact, but there are no stacks.
* `sample_errors` — the profiler failed to read a large share of samples.
* `memory_growth` — a test process's memory grew by at least 256 MB (and half of what it started with) over the
  run without being freed. `tests` lists the tests it grew most in, `growth` is the growth in bytes.

## Memory

Every test process measures its own memory around each test (setup to teardown), so under pytest-xdist each
worker measures the tests it runs. `peak_increase` (the peak above `start`) is how much memory the test needed;
`growth` (`end - start`) is how much it kept.

* **Linux**: the kernel's peak counter (`VmHWM`) is reset before every test by writing `5` to
  `/proc/self/clear_refs`, so every test gets its own peak. Private memory (`RssAnon`) leaves out the
  interpreter and shared libraries that every process maps, so processes can be added up for `machine_peak`.
* **macOS and Windows**: the peak can't be reset, so `peak` is only known for tests that raised their process's
  peak. With [psutil](https://pypi.org/project/psutil/) installed `start`, `end` and `growth` are known for
  every test; without it only the raised peaks are (macOS), or nothing (Windows). The values added up across
  workers are RSS, which counts shared memory once per process.
* `machine_peak` assumes each worker was at its test's peak for the whole test, so it is an upper bound, and
  the xdist controller process isn't included.
* Only the test processes are measured: memory used by a database server, a browser or subprocesses isn't.
* `growth` is noisy: Python and the C allocator often keep freed memory for reuse, so a test can show growth that
  later tests reuse. Peaks are reliable.
