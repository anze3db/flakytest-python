"""Alternate between a flat loop and a loop whose stack changes constantly; print the true time in each."""

import json
import sys
import time

seconds = float(sys.argv[1])
SLICE = 0.05


def flat():
    end = time.perf_counter() + SLICE
    total = 0
    while time.perf_counter() < end:
        for i in range(100):
            total += i * i
    return total


def leaf():
    return 1


def descend(n):
    return descend(n - 1) if n else leaf()


def churn():
    end = time.perf_counter() + SLICE
    while time.perf_counter() < end:
        descend(30)


truth = {"flat": 0.0, "churn": 0.0}
start = time.perf_counter()
while time.perf_counter() - start < seconds:
    for fn in (flat, churn):
        t = time.perf_counter()
        fn()
        truth[fn.__name__] += time.perf_counter() - t
print(json.dumps(truth))
