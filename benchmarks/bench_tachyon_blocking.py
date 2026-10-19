"""Benchmark hook: with BENCH_TACHYON_BLOCKING=1, flakytest's tachyon sampler pauses the target for each sample."""

import functools
import os
import sys


def install():
    if os.environ.get("BENCH_TACHYON_BLOCKING") != "1" or "flakytest.profiling.tachyon_sampler" not in sys.orig_argv:
        return
    from profiling.sampling import sample  # noqa: PLC0415 (only in the sampler process, never in the measured one)

    original = sample.SampleProfiler.__init__

    @functools.wraps(original)
    def init(self, *args, **kwargs):
        kwargs["blocking"] = True
        original(self, *args, **kwargs)

    sample.SampleProfiler.__init__ = init
