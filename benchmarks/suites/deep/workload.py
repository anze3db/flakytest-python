"""Deterministic CPU work with a known split between two functions, measured with perf_counter as ground truth."""

import json
import os
import time

TRUTH = os.environ.get("BENCH_TRUTH")


def work_a(n):
    # dict and string work
    d = {}
    for i in range(n):
        d[str(i)] = i
    return sum(len(k) for k in d)


def work_b(n):
    # arithmetic
    total = 0
    for i in range(n):
        total += (i * i) % 7
    return total


def descend(depth, fn):
    if depth:
        return descend(depth - 1, fn)
    return fn()


def run(nodeid, share_a, units, depth):
    """`units` chunks of work; share_a of them in work_a, the rest in work_b, each `depth` frames deep."""
    a_time = b_time = 0.0
    chunks_a = round(units * share_a)
    for i in range(units):
        start = time.perf_counter()
        if i < chunks_a:
            descend(depth, lambda: work_a(3000))
            a_time += time.perf_counter() - start
        else:
            descend(depth, lambda: work_b(6000))
            b_time += time.perf_counter() - start
    if TRUTH:
        with open(f"{TRUTH}.{os.getpid()}", "a") as f:
            f.write(json.dumps({"nodeid": nodeid, "a": a_time, "b": b_time}) + "\n")
