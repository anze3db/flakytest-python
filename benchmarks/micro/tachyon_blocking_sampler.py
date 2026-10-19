"""flakytest's tachyon sampler, but pausing the target's threads for each sample (SampleProfiler(blocking=True))."""

import sys

from profiling.sampling.sample import SampleProfiler

from flakytest.profiling import tachyon_sampler


def attach(pid, interval_usec):
    return SampleProfiler(pid, interval_usec, all_threads=False, blocking=True)


tachyon_sampler.attach = attach
sys.exit(tachyon_sampler.main())
