"""Run target_bias.py under each sampler and compare the sampled flat/churn split with the true one."""

import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
ENV = {"3.14": HERE.parent / "env3.14/bin", "3.15": HERE.parent / "env3.15/bin"}
CONFIGS = [("py-spy", 500), ("py-spy-blocking", 500), ("tachyon", 1000), ("tachyon-blocking", 1000)]


def shares(path: Path, sampler: str) -> dict:
    counts = {"flat": 0, "churn": 0}
    if sampler.startswith("tachyon"):
        data = json.loads(path.read_text())
        names = [f[1] for f in data["frames"]]
        for _, ids, count in data["stacks"]:
            stack = {names[i] for i in ids}
            for key in counts:
                if key in stack:
                    counts[key] += count
    else:
        for line in path.read_text().splitlines():
            stack, _, count = line.rpartition(" ")
            for key in counts:
                if f";{key} (" in stack:
                    counts[key] += int(count)
    return counts


def run(sampler, rate, reps):
    py = "3.14" if sampler.startswith("py-spy") else "3.15"
    bin_dir = ENV[py]
    results = []
    for _ in range(reps):
        target = subprocess.Popen([bin_dir / "python", HERE / "target_bias.py", "6"], stdout=subprocess.PIPE)
        out = HERE / "tmp" / f"bias-{target.pid}"
        if sampler.startswith("py-spy"):
            command = [
                bin_dir / "py-spy-real",
                "record",
                "--pid",
                str(target.pid),
                "--rate",
                str(rate),
                "--format",
                "raw",
                "--threads",
                "--idle",
                "--full-filenames",
                "--output",
                str(out),
            ]
            if sampler == "py-spy":
                command.append("--nonblocking")
        else:
            script = ["-m", "flakytest.profiling.tachyon_sampler"]
            if sampler == "tachyon-blocking":
                script = [HERE / "tachyon_blocking_sampler.py"]
            command = [
                bin_dir / "python",
                *script,
                "--pid",
                str(target.pid),
                "--rate",
                str(rate),
                "--out",
                str(out),
                "--ready",
                str(HERE / "tmp" / "ready"),
            ]
        sampler_process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        truth = json.loads(target.communicate()[0])
        sampler_process.wait()
        if not out.exists():
            results.append({"sampler": sampler, "crashed": True})
            continue
        counts = shares(out, sampler)
        out.unlink()
        sampled = counts["flat"] + counts["churn"]
        results.append(
            {
                "sampler": sampler,
                "rate": rate,
                "true_churn": truth["churn"] / (truth["flat"] + truth["churn"]) * 100,
                "sampled_churn": counts["churn"] / sampled * 100 if sampled else None,
                "flat_samples": counts["flat"],
                "churn_samples": counts["churn"],
            }
        )
    return results


if __name__ == "__main__":
    reps = int(sys.argv[1])
    with open(HERE / "bias.jsonl", "a") as f:
        for sampler, rate in CONFIGS:
            for result in run(sampler, rate, reps):
                f.write(json.dumps(result) + "\n")
                print(result, flush=True)
