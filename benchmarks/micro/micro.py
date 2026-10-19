"""Run target.py under each sampler configuration and record throughput, sampler CPU and samples captured.

usage: python micro.py OUT.jsonl REPS
"""

import json
import os
import random
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).parent
ENV = {"3.14": HERE.parent / "env3.14/bin", "3.15": HERE.parent / "env3.15/bin"}
SECONDS, WARMUP = 6.0, 1.0
DEPTHS = [10, 50, 150]


def configs():
    for depth in DEPTHS:
        for py in ("3.14", "3.15"):
            yield {"depth": depth, "py": py, "sampler": "none", "rate": 0}
        for rate in (100, 500, 1000, 2000):
            yield {"depth": depth, "py": "3.14", "sampler": "py-spy", "rate": rate}
            yield {"depth": depth, "py": "3.14", "sampler": "py-spy-blocking", "rate": rate}
        for rate in (100, 500, 1000, 2000, 5000):
            yield {"depth": depth, "py": "3.15", "sampler": "tachyon", "rate": rate}
        for rate in (500, 1000, 5000):
            yield {"depth": depth, "py": "3.15", "sampler": "tachyon-blocking", "rate": rate}


def count_samples(path: Path, sampler: str) -> int:
    if not path.exists():
        return 0
    if sampler.startswith("tachyon"):
        return sum(count for _, _, count in json.loads(path.read_text())["stacks"])
    total = 0
    for line in path.read_text().splitlines():
        stack, _, count = line.rpartition(" ")
        if stack:
            total += int(count)
    return total


def run(config, tmp: Path) -> dict:
    bin_dir = ENV[config["py"]]
    target = subprocess.Popen(
        [bin_dir / "python", HERE / "target.py", str(config["depth"]), str(SECONDS), str(WARMUP)],
        stdout=subprocess.PIPE,
    )
    out = tmp / f"samples-{target.pid}"
    sampler = None
    if config["sampler"].startswith("py-spy"):
        command = [
            bin_dir / "py-spy-real",
            "record",
            "--pid",
            str(target.pid),
            "--rate",
            str(config["rate"]),
            "--format",
            "raw",
            "--threads",
            "--idle",
            "--full-filenames",
            "--output",
            str(out),
        ]
        if config["sampler"] == "py-spy":
            command.append("--nonblocking")
        sampler = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    elif config["sampler"].startswith("tachyon"):
        module = ["-m", "flakytest.profiling.tachyon_sampler"]
        if config["sampler"] == "tachyon-blocking":
            module = [HERE / "tachyon_blocking_sampler.py"]
        command = [
            bin_dir / "python",
            *module,
            "--pid",
            str(target.pid),
            "--rate",
            str(config["rate"]),
            "--out",
            str(out),
            "--ready",
            str(tmp / "ready"),
        ]
        sampler = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    started = time.perf_counter()
    rate = json.loads(target.communicate()[0])["rate"]
    lifetime = time.perf_counter() - started
    result = {**config, "throughput": rate}
    if sampler:
        _, status, usage = os.wait4(sampler.pid, 0)
        sampler.returncode = status
        stderr = sampler.stdout.read().decode(errors="replace")
        samples = count_samples(out, config["sampler"])
        if config["sampler"].startswith("tachyon"):
            failed = json.loads(out.read_text())["failed"] if out.exists() else None
        else:
            match = re.search(r"Errors: (\d+)", stderr)
            failed = int(match.group(1)) if match else None
        result.update(
            sampler_cpu=usage.ru_utime + usage.ru_stime,
            samples=samples,
            achieved_rate=samples / lifetime,
            lifetime=lifetime,
            failed=failed,
            crashed=not out.exists(),
            stderr_tail=stderr.strip().splitlines()[-2:],
        )
        out.unlink(missing_ok=True)
    return result


def main():
    out_path, reps = Path(sys.argv[1]), int(sys.argv[2])
    tmp = out_path.parent / "tmp"
    tmp.mkdir(exist_ok=True)
    runs = [c for c in configs() for _ in range(reps)]
    random.seed(1)
    random.shuffle(runs)
    with out_path.open("a") as f:
        for i, config in enumerate(runs):
            result = run(config, tmp)
            f.write(json.dumps(result) + "\n")
            f.flush()
            print(f"{i + 1}/{len(runs)} {result}", flush=True)


if __name__ == "__main__":
    main()
