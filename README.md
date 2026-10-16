# flakytest

[![PyPI - Version](https://img.shields.io/pypi/v/flakytest.svg)](https://pypi.org/project/flakytest)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/flakytest.svg)](https://pypi.org/project/flakytest)

**See where your pytest suite spends its time, why, and what to fix first.**

flakytest runs your tests under a sampling profiler and tells you, for every test, class and file, how
much time goes to setup, the test itself and teardown, and inside those to the database, password
hashing, network calls, sleeps, fixtures, factories, imports… down to the line of your code that
causes it. Then it ranks what to fix, using the perf tricks for pytest, Django and SQLAlchemy that
usually take someone an afternoon of profiling to find.

It works locally, needs no account, and is built to be used by coding agents as much as by people.
[flakytest.dev](https://flakytest.dev) is optional: it collects the same profiles from every machine and
CI job, so you can see why CI is slower than your laptop, and it still tracks flaky tests.

```console
$ pip install flakytest
$ flakytest run
...
============================== flakytest profile ===============================
812 tests in 1.4 min (test time 5.0 min) · local:anze@mbp · Python 3.15.0 · tachyon @ 1000 Hz
Time goes to: password hashing 41% · database 30% · test code 9% · sleep 6%
Slowest tests:
    3.21s  tests/test_orders.py::TestCheckout::test_checkout  (database 70%, setUp (call) 2.1s, 412 queries)
Fix next:
  1. [high] Use a fast password hasher in tests (~2.0 min per run)
  2. [high] Create shared test data in setUpTestData instead of setUp (~41s per run)
  3. [high] Run tests in parallel on all 10 CPUs (~3.9 min per run)
Report: .flakytest/runs/20261016-210311-8c3f2a/index.html
```

## Install

```console
pip install flakytest
```

On **Python 3.15+** flakytest uses tachyon, the sampling profiler in the standard library. On older
Pythons it uses [py-spy](https://github.com/benfred/py-spy): `pip install 'flakytest[py-spy]'`. Without
either you still get exact durations, fixture times and query counts, just no breakdown inside tests.

## Profile your tests

```console
flakytest run                 # runs pytest with profiling and opens the report
flakytest run -- tests/api -x # anything after -- goes to pytest
pytest --flakytest-profile    # the same, from pytest itself (or FLAKYTEST_PROFILE=1 pytest)
```

Every run writes `.flakytest/runs/<run id>/` with:

* `index.html` — the report. A list of every test grouped by file and class; click a test or a group
  to see where its time goes: setup/call/teardown, time by category, the pieces of work (fixtures,
  `setUp`, the test body, Django's transaction setup…), the hot lines in your code with their source,
  a call tree (pytest internals hidden), hot functions, fixtures, SQL queries and repeated queries
  (N+1), and the suggestions that apply. `flakytest serve` serves it on localhost.
* `profile.json` — everything in the report, documented in [docs/profile-format.md](docs/profile-format.md).

`.flakytest/` ignores itself in git.

## What it suggests

Each suggestion comes with the time it costs per run, the lines of your code that trigger it, the tests
it affects, and how to make the change without making the tests less trustworthy. Among them:

| Problem | Fix |
|---|---|
| PBKDF2/bcrypt/argon2 password hashing | a fast hasher in test settings, `force_login` instead of `login` |
| Migrations on every run | `--reuse-db` / `--keepdb`, `--nomigrations` |
| `TransactionTestCase` flushing every table | `TestCase` (transaction rollback) |
| Data built in `setUp` for every test | `setUpTestData` (once per class) |
| Expensive function-scoped fixtures | wider scope, or an expensive shared part + cheap reset |
| SQLAlchemy `create_all`/`drop_all` per test | schema once per session, SAVEPOINT rollback per test |
| Repeated queries (N+1) | `select_related`/`prefetch_related`, `selectinload`, `assertNumQueries` |
| `time.sleep` in tests | poll for the condition, mock the backoff, travel in time |
| Real network calls | responses/respx/pytest-httpx, vcrpy, pytest-socket |
| Redis/Memcached, Celery, subprocesses, browsers | in-memory cache, eager tasks, direct calls, shared browser |
| Factories and Faker | `setUpTestData`, `build()`, bulk creation, one Faker instance |
| freezegun, mock autospec | time-machine, patch once per class |
| Heavy imports, slow collection | `testpaths`, `norecursedirs`, imports out of module level |
| GC pauses | `gc.freeze()` after startup, higher thresholds |
| Serial suites on multi-core machines | `pytest -n auto`, `--dist worksteal` |
| pytest-cov's default tracer | `COVERAGE_CORE=sysmon` |

## With coding agents

The report is written so an agent can act on it:

```console
flakytest report                      # Markdown: where time goes, ranked fixes with file:line, slowest tests
flakytest report --test tests/test_orders.py::TestCheckout   # one test or a group, with its call tree
flakytest report --format json        # the same as JSON
flakytest compare                     # previous vs latest run: faster/slower tests, and correctness
```

`flakytest compare` flags tests whose outcome changed and tests that stopped running, so a speed-up that
deletes, skips or breaks tests doesn't go unnoticed. The report's **Copy for agent** button copies what
you're looking at as Markdown.

`flakytest mcp` is an MCP server (stdio) over your local runs, with `get_report`, `get_test`,
`list_slow_tests`, `compare_runs` and `list_runs` tools:

```console
claude mcp add flakytest -- flakytest mcp
```

## Options

| Option | Environment | Default |
|---|---|---|
| `--flakytest-profile` | `FLAKYTEST_PROFILE=1` | off |
| `--flakytest-rate N` | `FLAKYTEST_PROFILE_RATE` | 1000 (tachyon), 500 (py-spy) |
| `--flakytest-backend auto\|tachyon\|py-spy\|none` | `FLAKYTEST_PROFILE_BACKEND` | `auto` |
| `--flakytest-output DIR` | `FLAKYTEST_PROFILE_DIR` | `<rootdir>/.flakytest/runs` |
| | `FLAKYTEST_ENV` | detected: `ci:github-actions:<job>`, `local:<user>@<host>`, … |

If tests are too short for the sampling rate, the report says so and suggests a rate: rerun with
`--flakytest-rate 5000` (tachyon reads the process from the outside, so high rates are cheap).

## How it works

pytest runs every test phase from `CallInfo.from_call`. flakytest wraps it so each phase runs inside a
tiny generated function named after the test and phase (`__flakytest_call_1f3a9c0d2b4e__`). A sampling
profiler attached to the pytest process (from a separate process, so it barely slows the tests down)
captures that frame in every stack it samples, which attributes each sample to a test and phase exactly,
with no clock synchronisation, for tachyon and py-spy alike, and per pytest-xdist worker. pytest's own
durations stay the source of truth; samples only decide how a phase's time is split.

Fixture setup times are measured exactly with a `pytest_fixture_setup` wrapper, and Django
(`execute_wrapper`) and SQLAlchemy (engine events) queries are counted per test.

**Permissions.** Sampling profilers read another process's memory. On Linux flakytest allows its own
profiler to attach (`prctl(PR_SET_PTRACER)`), which is enough with the default `ptrace_scope=1`
(including GitHub Actions). In Docker you may need `--cap-add SYS_PTRACE`. On macOS profilers need
root: `sudo -E flakytest run`. `flakytest doctor` checks your setup.

## flakytest.dev

Set `FLAKYTEST_SECRET_TOKEN` (create a project on [flakytest.dev](https://flakytest.dev)) and every
profiled run is uploaded, from developer machines and CI alike, so you can compare environments, see
trends and regressions, and query it all from agents via flakytest.dev's MCP server. The token also
turns on flaky test tracking for every run (profiled or not): failures are recorded per environment,
and muted tests on flakytest.dev don't fail your build.

```console
export FLAKYTEST_SECRET_TOKEN=****************************
pytest --flakytest-profile
```

What's uploaded is `profile.json`: test names, outcomes, durations, file paths relative to the project,
function names, source lines of the hot spots in your code, normalized SQL of repeated queries, the
environment name (`local:<user>@<host>` unless you set `FLAKYTEST_ENV`), git branch/commit and changed
file names. Set `FLAKYTEST_UPLOAD=0` to keep profiles local while still tracking flaky tests.

## License

`flakytest` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
