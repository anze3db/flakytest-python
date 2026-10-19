"""A busy single-threaded Python process with a fixed stack depth. Prints its throughput in a steady window."""

import json
import sys
import time

depth, seconds, warmup = int(sys.argv[1]), float(sys.argv[2]), float(sys.argv[3])


def work():
    total = 0
    for i in range(200):
        total += i * i
    return total


def descend(n):
    if n:
        return descend(n - 1)
    return work()


start = time.perf_counter()
iters = 0
mark_iters = mark_time = None
while True:
    descend(depth)
    iters += 1
    if iters % 50 == 0:
        now = time.perf_counter()
        if mark_time is None and now - start >= warmup:
            mark_iters, mark_time = iters, now
        if now - start >= seconds:
            break
print(json.dumps({"rate": (iters - mark_iters) / (now - mark_time)}))
