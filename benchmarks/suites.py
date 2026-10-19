"""Run whole pytest suites with and without flakytest profiling and record the cost and quality of each setup.

usage: python suites.py OUT.jsonl REPS
"""

import json
import os
import random
import shutil
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
PY = {"3.14": HERE / "env3.14/bin/python", "3.15": HERE / "env3.15/bin/python"}
SUITES = ["cpu", "deep", "django"]
TIMEOUT = 240


def configs():
    for suite in SUITES:
        for py in ("3.14", "3.15"):
            yield {"suite": suite, "py": py, "mode": "baseline"}
            yield {"suite": suite, "py": py, "mode": "none"}
        for rate in (100, 500, 1000):
            yield {"suite": suite, "py": "3.14", "mode": "py-spy", "rate": rate}
        yield {"suite": suite, "py": "3.14", "mode": "py-spy-blocking", "rate": 50}
        for rate in (500, 1000, 5000):
            yield {"suite": suite, "py": "3.15", "mode": "tachyon", "rate": rate}
        yield {"suite": suite, "py": "3.15", "mode": "tachyon-blocking", "rate": 1000}
    for py, mode, rate in [
        ("3.14", "baseline", 0),
        ("3.15", "baseline", 0),
        ("3.14", "py-spy", 500),
        ("3.15", "tachyon", 1000),
        ("3.15", "tachyon-blocking", 1000),
    ]:
        yield {"suite": "cpu", "py": py, "mode": mode, "rate": rate, "xdist": 4}


def accuracy(doc, truth):
    """Mean absolute error (percentage points) of the estimated work_a share per test, and tests without samples."""
    frames = doc["frames"]
    nodes = doc["stacks"]
    kind = {}

    def classify(node):
        if node in kind:
            return kind[node]
        parent, frame = nodes[node]
        name = frames[frame][1].rsplit(".", 1)[-1]
        result = name if name in ("work_a", "work_b") else (classify(parent) if parent >= 0 else None)
        kind[node] = result
        return result

    errors, missing = [], 0
    for test in doc["tests"]:
        real = truth.get(test["nodeid"])
        if not real:
            continue
        counts = defaultdict(int)
        for node, count in test["stacks"].get("call", []):
            counts[classify(node)] += count
        sampled = counts["work_a"] + counts["work_b"]
        if not sampled:
            missing += 1
            continue
        estimated = counts["work_a"] / sampled
        errors.append(abs(estimated - real["a"] / (real["a"] + real["b"])) * 100)
    return (sum(errors) / len(errors) if errors else None), missing


def run(config):
    suite_dir = HERE / "suites" / config["suite"]
    shutil.rmtree(suite_dir / ".flakytest", ignore_errors=True)
    out = HERE / "tmp-bench.json"
    truth = HERE / "tmp-truth"
    for stale in [out, *HERE.glob("tmp-truth.*")]:
        stale.unlink(missing_ok=True)
    command = [str(PY[config["py"]]), "-m", "pytest", "-q"]
    if config.get("xdist"):
        command += ["-n", str(config["xdist"])]
    env = {**os.environ, "BENCH_OUT": str(out), "BENCH_TRUTH": str(truth), "FLAKYTEST_UPLOAD": "0"}
    env.pop("FLAKYTEST_SECRET_TOKEN", None)
    if config["mode"] != "baseline":
        command.append("--flakytest-profile")
        backend = {"none": "none", "py-spy": "py-spy", "py-spy-blocking": "py-spy"}.get(config["mode"], "tachyon")
        command += ["--flakytest-backend", backend]
        if config.get("rate"):
            command += ["--flakytest-rate", str(config["rate"])]
    if config["mode"] == "py-spy-blocking":
        env["BENCH_PYSPY_BLOCKING"] = "1"
    if config["mode"] == "tachyon-blocking":
        env["BENCH_TACHYON_BLOCKING"] = "1"
    started = time.perf_counter()
    log = HERE / "tmp-output.txt"
    with log.open("wb") as f:
        process = subprocess.Popen(
            command, cwd=suite_dir, env=env, stdout=f, stderr=subprocess.STDOUT, start_new_session=True
        )
        deadline = started + TIMEOUT
        while time.perf_counter() < deadline:
            pid, status, usage = os.wait4(process.pid, os.WNOHANG)
            if pid:
                break
            time.sleep(0.05)
        else:
            # The samplers run in their own sessions: kill them too, or a ptrace-paused pytest stays stopped
            subprocess.run(["pkill", "-9", "-f", "py-spy-real|tachyon_sampler|tachyon_blocking_sampler"], check=False)
            os.killpg(process.pid, 9)
            os.wait4(process.pid, 0)
            return {**config, "ok": False, "timeout": True, "wall": time.perf_counter() - started}
    wall = time.perf_counter() - started
    output = log.read_text(errors="replace")
    marks = json.loads(out.read_text())
    result = {
        **config,
        "ok": status == 0,
        "wall": wall,
        "tests_wall": marks["marks"]["tests_end"] - marks["marks"]["tests_start"],
        "after_tests": marks["marks"]["end"] - marks["marks"]["tests_end"],
        "total_cpu": usage.ru_utime + usage.ru_stime,
        "self_cpu": marks["self_cpu"],
        "children_cpu": marks["children_cpu"],
    }
    if status != 0:
        result["output_tail"] = output.strip().splitlines()[-5:]
    profiles = list((suite_dir / ".flakytest/runs").glob("*/profile.json"))
    profile = profiles[0] if profiles else None
    if config["mode"] != "baseline" and profile:
        doc = json.loads(profile.read_text())
        profiler = doc["run"]["profiler"]
        tests = doc["tests"]
        rate = profiler["rate"] or 1
        result.update(
            samples=profiler["samples"],
            sample_errors=profiler.get("sample_errors", 0),
            backend=profiler["backend"],
            test_time=doc["summary"]["test_time"],
            unsampled_tests=sum(1 for t in tests if sum(t["samples"].values()) == 0 and t["duration"] >= 3 / rate),
            under5_tests=sum(1 for t in tests if sum(t["samples"].values()) < 5),
            timeline_tests=doc["summary"]["timeline"].get("tests"),
        )
        if config["suite"] in ("cpu", "deep"):
            truth_rows = {}
            for path in HERE.glob("tmp-truth.*"):
                for line in path.read_text().splitlines():
                    row = json.loads(line)
                    truth_rows[row["nodeid"]] = row
            result["share_error_pp"], result["no_work_samples"] = accuracy(doc, truth_rows)
    return result


def key(row):
    return (row["suite"], row["py"], row["mode"], row.get("rate") or 0, row.get("xdist") or 0)


def main():
    out_path, reps = Path(sys.argv[1]), int(sys.argv[2])
    only = sys.argv[3:] or None
    done = defaultdict(int)
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            row = json.loads(line)
            done[key(row)] += 1
    runs = []
    for c in configs():
        if not only or c["suite"] in only:
            runs += [c] * max(0, reps - done[key(c)])
    random.seed(2)
    random.shuffle(runs)
    with out_path.open("a") as f:
        for i, config in enumerate(runs):
            result = run(config)
            f.write(json.dumps(result) + "\n")
            f.flush()
            print(f"{i + 1}/{len(runs)} {json.dumps(result)}", flush=True)


if __name__ == "__main__":
    main()
