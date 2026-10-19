"""Attach tachyon (blocking) to a pid for a few seconds and print the distribution of sampled stack depths."""

import statistics
import sys

from profiling.sampling.sample import SampleProfiler

from flakytest.profiling.tachyon_sampler import FullStackCollector

profiler = SampleProfiler(int(sys.argv[1]), 1000, all_threads=False, blocking=True)
collector = FullStackCollector(1000)
profiler.sample(collector, duration_sec=4)
depths = [len(ids) for (_, ids), count in collector.stacks.items() for _ in range(count)]
print("median", statistics.median(depths), "p90", sorted(depths)[int(len(depths) * 0.9)], "max", max(depths))
