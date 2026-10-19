"""Summarise micro/results.jsonl, suites.jsonl and micro/bias.jsonl as Markdown tables (medians of the repetitions)."""

import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent


def load(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def micro():
    rows = load(HERE / "micro/results.jsonl")
    base = {}
    for row in rows:
        if row["sampler"] == "none":
            base.setdefault((row["depth"], row["py"]), []).append(row["throughput"])
    base = {k: st.median(v) for k, v in base.items()}
    groups = defaultdict(list)
    for row in rows:
        if row["sampler"] != "none":
            groups[(row["depth"], row["sampler"], row["rate"])].append(row)
    print(
        "| Stack depth | Sampler | Requested Hz | Target slowdown | Good samples/s | Failed | Sampler CPU | Crashed |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for (depth, sampler, rate), rs in sorted(groups.items()):
        py = "3.14" if sampler.startswith("py-spy") else "3.15"
        slowdown = (1 - st.median(r["throughput"] for r in rs) / base[(depth, py)]) * 100
        ok = [r for r in rs if not r["crashed"]]
        good = st.median(r["achieved_rate"] for r in rs)
        failed = None
        if ok:
            failed = st.median((r["failed"] or 0) / max(1, r["samples"] + (r["failed"] or 0)) * 100 for r in ok)
        cpu = st.median(r["sampler_cpu"] / r["lifetime"] * 100 for r in rs)
        crashed = len(rs) - len(ok)
        print(
            f"| {depth} | {sampler} | {rate} | {slowdown:.0f}% | {good:.0f} ({good / rate:.0%}) | "
            f"{'n/a' if failed is None else f'{failed:.0f}%'} | {cpu:.0f}% | {crashed}/{len(rs)} |"
        )


def suites():
    rows = load(HERE / "suites.jsonl")
    base = {}
    for row in rows:
        if row["mode"] == "baseline" and row.get("ok"):
            base.setdefault((row["suite"], row["py"], row.get("xdist") or 0), []).append(row["wall"])
    base = {k: st.median(v) for k, v in base.items()}
    groups = defaultdict(list)
    for row in rows:
        groups[(row["suite"], row.get("xdist") or 0, row["py"], row["mode"], row.get("rate") or 0)].append(row)
    print(
        "| Suite | Python | Mode | Hz | Wall | Overhead | Profiler CPU | Samples/s of test time | Tests <5 samples "
        "| Split error (pp) | Runs |"
    )
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for (suite, xdist, py, mode, rate), rs in sorted(groups.items()):
        ok = [r for r in rs if r.get("ok")]
        name = f"{suite} -n {xdist}" if xdist else suite
        if not ok:
            print(f"| {name} | {py} | {mode} | {rate} | timeout | | | | | | {len(rs)} |")
            continue
        wall = st.median(r["wall"] for r in ok)
        overhead = (wall / base[(suite, py, xdist)] - 1) * 100
        cpu = st.median(r["total_cpu" if xdist else "children_cpu"] for r in ok)
        sampled = [r for r in ok if r.get("samples") is not None and r.get("test_time")]
        rate_cell = under = err = ""
        if sampled and rate:
            per_test_s = st.median(r["samples"] / r["timeline_tests"] for r in sampled if r.get("timeline_tests"))
            rate_cell = f"{per_test_s:.0f} ({per_test_s / rate:.0%})" if not xdist else f"{per_test_s:.0f}"
            under = f"{st.median(r['under5_tests'] for r in sampled):.0f}"
            errs = [r["share_error_pp"] for r in sampled if r.get("share_error_pp") is not None]
            err = f"{st.median(errs):.1f}" if errs else ""
        timeouts = len(rs) - len(ok)
        print(
            f"| {name} | {py} | {mode} | {rate or ''} | {wall:.1f}s | {overhead:+.0f}% | {cpu:.1f}s | {rate_cell} | "
            f"{under} | {err} | {len(ok)}{f' (+{timeouts} timed out)' if timeouts else ''} |"
        )


def bias():
    rows = load(HERE / "micro/bias.jsonl")
    groups = defaultdict(list)
    for row in rows:
        groups[(row["sampler"], row.get("rate"))].append(row)
    print("| Sampler | Hz | True churn share | Sampled churn share | Runs |")
    print("|---|---|---|---|---|")
    for (sampler, rate), rs in groups.items():
        ok = [r for r in rs if not r.get("crashed") and r.get("sampled_churn") is not None]
        if not ok:
            print(f"| {sampler} | {rate} | | crashed | {len(rs)} |")
            continue
        print(
            f"| {sampler} | {rate} | {st.median(r['true_churn'] for r in ok):.0f}% | "
            f"{st.median(r['sampled_churn'] for r in ok):.0f}% | {len(ok)}/{len(rs)} |"
        )


if __name__ == "__main__":
    for name in sys.argv[1:] or ["micro", "suites", "bias"]:
        print(f"\n## {name}\n")
        globals()[name]()
