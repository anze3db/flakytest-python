"""Sample a Python 3.15+ process with tachyon (profiling.sampling) and write every stack with full paths.

Run by the pytest plugin as `python -m flakytest.profiling.tachyon_sampler --pid PID --rate HZ --out FILE`.
SIGINT or SIGTERM stops sampling and writes the output; so does the target process exiting.
"""

import argparse
import json
import os
import signal
import sys
import time
from collections import Counter
from pathlib import Path

from profiling.sampling.collector import extract_lineno  # type: ignore[import-not-found]
from profiling.sampling.sample import SampleProfiler  # type: ignore[import-not-found]
from profiling.sampling.stack_collector import StackTraceCollector  # type: ignore[import-not-found]

ATTACH_RETRIES = 40


class FullStackCollector(StackTraceCollector):
    def __init__(self, sample_interval_usec: int) -> None:
        super().__init__(sample_interval_usec, skip_idle=False)
        self.frame_ids: dict[tuple[str, str, int], int] = {}
        self.stacks: Counter[tuple[int, tuple[int, ...]]] = Counter()
        self.failed = 0

    def process_frames(self, frames, thread_id, weight=1):
        ids = []
        for frame in reversed(frames):
            key = (frame[0], frame[2], extract_lineno(frame[1]))
            frame_id = self.frame_ids.get(key)
            if frame_id is None:
                frame_id = self.frame_ids[key] = len(self.frame_ids)
            ids.append(frame_id)
        self.stacks[(thread_id, tuple(ids))] += weight

    def collect_failed_sample(self):
        self.failed += 1

    def export(self, filename):
        data = {
            "backend": "tachyon",
            "frames": list(self.frame_ids),
            "stacks": [[tid, list(ids), count] for (tid, ids), count in self.stacks.items()],
            "failed": self.failed,
        }
        Path(filename).write_text(json.dumps(data))


def attach(pid: int, interval_usec: int) -> SampleProfiler:
    # The target allows us to attach with prctl(PR_SET_PTRACER) right after starting us, retry until it has
    for attempt in range(ATTACH_RETRIES):
        try:
            return SampleProfiler(pid, interval_usec, all_threads=False)
        except (PermissionError, SystemExit, RuntimeError, OSError):
            if attempt == ATTACH_RETRIES - 1:
                raise
            time.sleep(0.05)
    raise AssertionError


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ready", required=True)
    args = parser.parse_args()

    interval_usec = max(1, 1_000_000 // args.rate)
    collector = FullStackCollector(interval_usec)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        profiler = attach(args.pid, interval_usec)
    except BaseException as e:
        Path(args.ready).write_text(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    Path(args.ready).write_text(json.dumps({"ok": True, "pid": os.getpid()}))
    started = time.perf_counter()
    try:
        profiler.sample(collector)
    except KeyboardInterrupt:
        pass
    collector.export(args.out)
    sys.stdout.write(f"sampled {sum(collector.stacks.values())} stacks in {time.perf_counter() - started:.2f}s\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
