# flakytest

[![PyPI - Version](https://img.shields.io/pypi/v/flakytest.svg)](https://pypi.org/project/flakytest)
[![PyPI - Python Version](https://img.shields.io/pypi/pyversions/flakytest.svg)](https://pypi.org/project/flakytest)

**See where your pytest suite spends its time, why, and what to fix first.**

Put `flakytest` in front of your pytest command. It runs your tests under a sampling profiler and tells you, for
every test, class and file, how much time goes to setup, the test itself and teardown, and inside those to the
database, password hashing, network calls, sleeps, fixtures, factories, imports… down to the line of your code that
causes it. Then it ranks what to fix, using the perf tricks for pytest, Django and SQLAlchemy that usually take
someone an afternoon of profiling to find.

Everything stays on your machine: no account, no upload. The report is a local HTML page, and everything in it is
also available as Markdown and JSON for coding agents.

```console
$ pip install flakytest
$ flakytest pytest
...
============================== flakytest profile ===============================
7 tests in 2.35s (test time 2.03s) · local:dev@laptop · Python 3.15.0 · tachyon @ 1000 Hz, 2,049 samples
Time goes to: password hashing 71% · sleep 20% · DB schema / flush 4% · database 3%
Slowest tests:
    485ms  shop/tests/test_accounts.py::TestLogin::test_profile_page  (password hashing 100%, test body in call 243ms, 22 queries)
    475ms  shop/tests/test_accounts.py::TestLogin::test_wrong_password  (password hashing 99%, test body in call 239ms, 6 queries)
    402ms  shop/tests/test_orders.py::test_warehouse_is_notified  (sleep 100%, test body in call 401ms, 2 queries)
     44ms  shop/tests/test_orders.py::TestCheckout::test_order_list  (database 98%, test body in call 31ms, 265 queries)
Fix next:
  1. [high] Use a fast password hasher in tests (~1.37s per run)
  2. [high] Create shared test data in setUpTestData instead of setUp (~530ms per run)
  3. [high] Tests are sleeping (~361ms per run)
Memory: peak 68 MB · most: shop/tests/test_accounts.py::TestLogin::test_profile_page needed 372 KB
Report: .flakytest/runs/20261027-224652-0223c7/index.html
  flakytest serve  ·  flakytest report --format markdown  (for agents)
============================== 7 passed in 2.11s ===============================
```

![The local report: where a run's test time goes, by category, with the session timeline](https://raw.githubusercontent.com/anze3db/flakytest-python/main/docs/images/report-overview.png)

## Install

```console
pip install flakytest
```

On **Python 3.15+** flakytest uses tachyon, the sampling profiler in the standard library. On older
Pythons it uses [py-spy](https://github.com/benfred/py-spy): `pip install 'flakytest[py-spy]'`. Without
either you still get exact durations, fixture times and query counts, just no breakdown inside tests.

## Profile your tests

Run pytest the way you always do, with `flakytest` in front:

```console
flakytest pytest                        # the whole suite
flakytest pytest tests/api -x           # every argument goes to pytest
flakytest pytest -n auto                # pytest-xdist works too: each worker is profiled
python -m flakytest pytest              # the same, without the console script
```

Plain `pytest` is untouched: flakytest only does something when you run your tests through it.

Every run writes `.flakytest/runs/<run id>/` with:

* `index.html` — the report. A list of every test grouped by file and class; click a test or a group
  to see where its time goes: setup/call/teardown, time by category, the pieces of work (fixtures,
  `setUp`, the test body, Django's transaction setup…), the hot lines in your code with their source,
  a call tree (pytest internals hidden), hot functions, fixtures, SQL queries and repeated queries
  (N+1), memory, and the suggestions that apply. Open it from disk, or with `flakytest serve`.
* `profile.json` — everything in the report, documented in [docs/profile-format.md](https://github.com/anze3db/flakytest-python/blob/main/docs/profile-format.md).
  `profile.js` is the same data for `index.html`, and `summary.json` is what the next run compares itself with.

`.flakytest/` ignores itself in git.

![One test in the report: where its time goes, its memory, and what to fix, down to the line](https://raw.githubusercontent.com/anze3db/flakytest-python/main/docs/images/report-test.png)

## Memory

Every test's memory is measured too, in whichever process runs it (each pytest-xdist worker measures its
own tests): how much it needed above what its process already had (its peak), and how much it kept. The
report lists the tests that needed the most (the **Most memory** filter in the HTML report), each worker's
peak, and on Linux all workers added up over time, which is what a CI machine needs to avoid running out of
memory. A process that keeps growing gets a `memory_growth` warning naming the tests it grew in.

What a test needed and kept is private memory, measured from before its setup until the next test starts. A
class, module or session fixture keeps its own memory (and its own queries): the test that happened to run it
first isn't charged for it. The first test in each process and class, and tests that set up such a fixture, are
left out of the tests that needed the most, since they pay for warming up.

Measuring costs well under a millisecond per test. Limitations:

* Exact per-test peaks need Linux, where the kernel's peak counter can be reset per test. On macOS and
  Windows a test's peak is only known when it raised its process's peak, and `pip install psutil` adds the
  memory at the start and end of every test.
* Only the test processes are measured: a database server, a browser or subprocesses aren't included.
* "Kept" is noisy, since Python and the C allocator often keep freed memory for reuse; peaks are reliable.

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

These are pytest options, so they go with the rest: `flakytest pytest --flakytest-rate 5000 tests/test_api.py`.

| Option | Environment | Default |
|---|---|---|
| `--flakytest-rate N` | `FLAKYTEST_PROFILE_RATE` | 1000 (tachyon; 500 above 10,000 tests), 500 (py-spy; 100 when pytest-xdist workers use every CPU) |
| `--flakytest-backend auto\|tachyon\|py-spy\|none` | `FLAKYTEST_PROFILE_BACKEND` | `auto` |
| `--flakytest-output DIR` | `FLAKYTEST_PROFILE_DIR` | `<rootdir>/.flakytest/runs` |
| | `FLAKYTEST_ENV` | the run's environment name: `local:<user>@<host>`, `ci:github-actions:<job>`, … |

If tests are too short for the sampling rate, the report says so and suggests a rate: rerun the tests you care
about with it, e.g. `flakytest pytest --flakytest-rate 5000 tests/test_api.py`. Suite-wide numbers and suggestions don't
need a high rate; a long test is sampled at a quarter of the rate once it has 200 samples.

## How it works

pytest runs every test phase from `CallInfo.from_call`. flakytest wraps it so each phase runs inside a
tiny generated function named after the test and phase (`__flakytest_call_1f3a9c0d2b4e__`). A sampling
profiler attached to the pytest process (from a separate process, at a lower priority than the tests)
captures that frame in every stack it samples, which attributes each sample to a test and phase exactly,
with no clock synchronisation, for tachyon and py-spy alike, and per pytest-xdist worker. pytest's own
durations stay the source of truth; samples only decide how a phase's time is split. With tachyon, other
threads are sampled while the main thread waits, so work in a thread pool shows up as that work, not as
"waiting".

Each test process builds its part of the profile when it finishes (pytest-xdist workers at the same time) and
the controller merges them. Every worker records its tests as they finish, so when one crashes the report
says so and keeps them (or takes them from pytest's reports).

Fixture setup times are measured exactly with a `pytest_fixture_setup` wrapper, and Django
(`execute_wrapper`) and SQLAlchemy (engine events) queries are counted per test.

**Permissions.** Sampling profilers read another process's memory. On Linux flakytest allows its own
profiler to attach (`prctl(PR_SET_PTRACER)`), which is enough with the default `ptrace_scope=1`
(including GitHub Actions). In Docker you may need `--cap-add SYS_PTRACE`. On macOS profilers need
root: `sudo -E flakytest pytest`, or run the tests in a Linux container, which also gets you exact per-test memory:

```console
docker run --rm -it --cap-add SYS_PTRACE -v "$PWD":/src -w /src python:3.15 \
  sh -c "pip install -r requirements.txt flakytest && flakytest pytest"
```

(with your project's install step). Tests that use services on your Mac reach them at `host.docker.internal`, or
with `--network host` where your Docker supports it. `flakytest doctor` checks your setup.

## Optional: flakytest.dev

Everything above works locally and needs no account. [flakytest.dev](https://flakytest.dev) is an optional
service on top: set `FLAKYTEST_SECRET_TOKEN` (from a project there) and `flakytest pytest` uploads each run's
profile, from developer machines and CI alike, so you can compare environments and see trends over time. The
token also turns on flaky test tracking: failures are recorded per environment, and tests muted on flakytest.dev
don't fail your build.

```console
export FLAKYTEST_SECRET_TOKEN=****************************
flakytest pytest
```

What's uploaded is `profile.json`: test names, outcomes, durations, file paths relative to the project,
function names, source lines of the hot spots in your code, normalized SQL of repeated queries, the
environment name (`local:<user>@<host>` unless you set `FLAKYTEST_ENV`), git branch/commit and changed
file names. Set `FLAKYTEST_UPLOAD=0` to keep profiles local while still tracking flaky tests. Its
[documentation](https://flakytest.dev/docs/) covers CI setup, flaky test classification and data retention.

## License

`flakytest` is distributed under the terms of the [MIT](https://spdx.org/licenses/MIT.html) license.
