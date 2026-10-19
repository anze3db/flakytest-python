"""Attach tachyon (as flakytest does) to a running pid for a few seconds and print its own statistics."""

import sys

from profiling.sampling.sample import SampleProfiler

from flakytest.profiling.tachyon_sampler import FullStackCollector

pid, blocking = int(sys.argv[1]), sys.argv[2] == "blocking"
profiler = SampleProfiler(pid, 1000, all_threads=False, collect_stats=True, blocking=blocking)
collector = FullStackCollector(1000)
profiler.sample(collector, duration_sec=4)
print("stacks", sum(collector.stacks.values()), "failed", collector.failed)
