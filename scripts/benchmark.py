"""Check flakytest's overhead budget on a generated suite.

    uv run scripts/benchmark.py [--tests 5000] [--rounds 3] [--workers N]

Runs the suite without flakytest, with exact timings only (--flakytest-backend none) and sampled, interleaved,
and fails when:
- building the profile after the last test takes more than 10% of the sampled run, or
- exact mode makes the run more than 5% slower (fastest round of each, to keep noise out).
"""

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from flakytest.profiling.samplers import choose_backend

CONFTEST = """
import json
import pytest

@pytest.fixture(scope="session")
def catalog():
    return [{"id": i, "name": f"item {i}", "tags": ["a", "b"]} for i in range(2000)]

@pytest.fixture
def account(catalog):
    return {"items": catalog[:20], "balance": 100}

@pytest.fixture
def payload(account):
    return json.dumps(account)
"""

TEST_FILE = """
import json
import pytest

def work(n):
    total = 0
    for i in range(n):
        total += i * i % 7
    return total

@pytest.mark.parametrize("i", range({count}))
def test_compute(i, account, payload):
    assert work(300_000) >= 0  # ~10 ms, a fast database test (a large Django suite averaged 37 ms per test)
    assert json.loads(payload)["balance"] == account["balance"]

class TestThings:
    @pytest.mark.parametrize("i", range({count}))
    def test_skipped_half(self, i):
        if i % 2:
            pytest.skip("flags-off")
        assert work(150_000) >= 0
"""

PER_FILE = 50  # tests of each kind per file


def write_suite(root: Path, tests: int) -> None:
    (root / "conftest.py").write_text(CONFTEST)
    (root / "pytest.ini").write_text("[pytest]\n")
    for index in range(max(1, tests // (2 * PER_FILE))):
        (root / f"test_suite_{index:03}.py").write_text(TEST_FILE.format(count=PER_FILE))


def run(root: Path, args: list[str]) -> float:
    """Seconds a pytest run took. args start with "pytest" (plain pytest) or "flakytest" (flakytest pytest)."""
    runner = ["-m", "pytest"] if args[0] == "pytest" else ["-m", "flakytest", "pytest"]
    started = time.perf_counter()
    result = subprocess.run(
        [sys.executable, *runner, "-q", "-p", "no:cacheprovider", *args[1:]],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        sys.stderr.write(result.stdout[-3000:] + result.stderr[-3000:])
        raise SystemExit(f"pytest failed: {' '.join(args)}")
    return elapsed


def postprocessing(root: Path) -> tuple[float, float, dict]:
    """(seconds from the last test to the files being written, seconds the whole session took, the profile's
    own timings) for the latest run."""
    runs = root / ".flakytest" / "runs"
    run_dir = runs / (runs / "latest").read_text().strip()
    profile = json.loads((run_dir / "profile.json").read_text())
    started = dt.datetime.fromisoformat(profile["run"]["started_at"].replace("Z", "+00:00")).timestamp()
    written = (run_dir / "summary.json").stat().st_mtime
    timings = profile["run"]["postprocessing"]
    return written - (started + timings["tests_finished_at"]), written - started, timings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tests", type=int, default=5000)
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--workers", type=int, default=0, help="pytest-xdist workers (-n), 0 for none")
    parser.add_argument("--max-postprocessing", type=float, default=0.10)
    parser.add_argument("--max-exact-overhead", type=float, default=0.05)
    args = parser.parse_args()

    sampled = choose_backend("auto")[0] != "none"
    xdist = ["-n", str(args.workers)] if args.workers else []
    variants = {
        "baseline": ["pytest", *xdist],
        "exact": ["flakytest", "--flakytest-backend", "none", *xdist],
    }
    if sampled:
        variants["sampled"] = ["flakytest", *xdist]
    times: dict[str, list[float]] = {name: [] for name in variants}
    post: list[tuple[float, float, dict]] = []
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        write_suite(root, args.tests)
        run(root, [*variants["baseline"], "--collect-only"])  # warm up imports and the filesystem cache
        for round_ in range(args.rounds):
            for name, extra in variants.items():
                times[name].append(run(root, extra))
                if name == "sampled":
                    post.append(postprocessing(root))
            print(f"round {round_ + 1}: " + ", ".join(f"{name} {values[-1]:.2f}s" for name, values in times.items()))

    best = {name: min(values) for name, values in times.items()}
    exact_overhead = best["exact"] / best["baseline"] - 1
    print(f"exact mode: {best['exact']:.2f}s vs {best['baseline']:.2f}s without flakytest ({exact_overhead:+.1%})")
    failures = []
    if exact_overhead > args.max_exact_overhead:
        failures.append(f"exact mode overhead {exact_overhead:.1%} > {args.max_exact_overhead:.0%}")
    if post:
        seconds, session, timings = min(post, key=lambda p: p[0] / p[1])
        share = seconds / session
        print(
            f"sampled: {best['sampled']:.2f}s ({best['sampled'] / best['baseline'] - 1:+.1%}); building the profile "
            f"took {seconds:.2f}s after the last test, {share:.1%} of the run (parts {timings['parts']:.2f}s, "
            f"merge {timings['merge']:.2f}s)"
        )
        if share > args.max_postprocessing:
            failures.append(f"post-processing {share:.1%} of the run > {args.max_postprocessing:.0%}")
    else:
        print("no sampling profiler here: skipped the post-processing budget")
    for failure in failures:
        print(f"FAILED: {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
