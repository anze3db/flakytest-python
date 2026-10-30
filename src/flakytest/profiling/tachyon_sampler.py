"""Sample a Python 3.15+ process with tachyon (profiling.sampling) and stream its stacks to a file.

Run by the pytest plugin as `python -m flakytest.profiling.tachyon_sampler --pid PID --rate HZ --out FILE ...`.
SIGINT or SIGTERM stops sampling and writes what's left; so does the target process exiting.

Stacks are stored as a prefix tree as they are sampled, and written every few seconds as one JSON line of what's
new since the last write, so the sampler never holds (or serialises) the whole run at once, and a crash keeps
everything up to the last write. Every test's stacks are new to the tree (they're below its marker frame), so it
would grow with the run: once it's big, the sampler forgets it and starts a new one, whose frames and nodes get new
ids (the reader doesn't mind seeing a frame or stack twice). Each line is

    {"frames": [[filename, function, line], ...],     # new frames, ids continue from the previous lines
     "nodes": [[parent node or -1, frame], ...],      # new stack nodes, likewise
     "main": [[node, weight], ...],                   # the main thread's samples since the last line
     "threads": [[marker frame, thread id, node, weight], ...],
     "failed": 3,
     "seconds": 12.5}                                 # how long it has been sampling

Other threads are sampled while the main thread isn't running Python (waiting on a thread pool, a lock or I/O),
and tied to the test phase the main thread is in at that instant by the main thread's marker frame. The last line
is {"done": true, ...}.

Weights count samples at the requested rate: when the plugin lowers the rate (a long test, a big suite), each
sample weighs more, so time is still split correctly.
"""

import argparse
import contextlib
import json
import mmap
import os
import signal
import struct
import sys
import time
from pathlib import Path

from profiling.sampling.sample import SampleProfiler  # type: ignore[import-not-found]

ATTACH_RETRIES = 40
FLUSH_SECONDS = 5.0
FLUSH_ENTRIES = 200_000
# Stack nodes and frames the sampler remembers before it starts over (~25 MB)
FORGET_AFTER = 100_000
MARKER_FILENAME = "<flakytest>"  # flakytest.profiling.markers, not imported to keep the sampler lean
# The control file the plugin writes: test sequence number, then the sampling interval it wants in microseconds
CONTROL = struct.Struct("<QI")
# After this many samples of one test, sample it at a quarter of the rate: its breakdown is settled
SAMPLES_PER_TEST = 200
SLOW_DOWN = 4
# Unwinding every thread is skipped this many samples after a sample that found no other threads
NO_THREADS_BACKOFF = 50
# After this many failed samples in a row, check whether the target is still alive
GONE_CHECK = 20
THREAD_STATUS_HAS_GIL = 1 << 0
THREAD_STATUS_ON_CPU = 1 << 1
THREAD_STATUS_MAIN_THREAD = 1 << 5


class StackWriter:
    def __init__(self, out: Path) -> None:
        self.file = out.open("a", encoding="utf-8")
        self.frame_ids: dict[tuple[str, str, int], int] = {}
        self.by_object: dict[int, tuple[object, int, bool]] = {}  # id(FrameInfo) -> (it, frame id, is a marker)
        self.trie: dict[int, int] = {}
        self.frame_count = 0
        self.node_count = 0
        self.new_frames: list[tuple[str, str, int]] = []
        self.new_nodes: list[tuple[int, int]] = []
        self.main: dict[int, int] = {}
        self.threads: dict[tuple[int, int, int], int] = {}
        # thread id -> (frames, nodes, marker frame id, marker position) of its previous sample
        self.previous: dict[int, tuple[list, list[int], int, int]] = {}
        self.failed = 0
        self.samples = 0
        self.flushed_at = time.monotonic()
        self.started = time.perf_counter()

    def frame_id(self, frame) -> tuple[int, bool]:
        entry = self.by_object.get(id(frame))
        if entry is not None and entry[0] is frame:
            return entry[1], entry[2]
        location = frame[1]
        line = 0 if location is None else location if isinstance(location, int) else location[0]
        key = (frame[0], frame[2], line or 0)
        frame_id = self.frame_ids.get(key)
        if frame_id is None:
            frame_id = self.frame_ids[key] = self.frame_count
            self.frame_count += 1
            self.new_frames.append(key)
        marker = frame[0] == MARKER_FILENAME
        self.by_object[id(frame)] = (frame, frame_id, marker)
        return frame_id, marker

    def insert(self, thread_id: int, leaf_first) -> tuple[int, int]:
        """The stack's leaf node, and its marker frame id (-1 without one)."""
        frames = leaf_first[::-1]
        start, nodes, marker, marker_at = 0, [], -1, -1
        previous = self.previous.get(thread_id)
        if previous is not None:
            old_frames, old_nodes, old_marker, old_marker_at = previous
            limit = min(len(frames), len(old_frames))
            while start < limit and frames[start] is old_frames[start]:
                start += 1
            nodes = old_nodes[:start]
            if old_marker_at < start:
                marker, marker_at = old_marker, old_marker_at
        parent = nodes[-1] if nodes else -1
        trie = self.trie
        for position in range(start, len(frames)):
            frame_id, is_marker = self.frame_id(frames[position])
            if is_marker and marker < 0:
                marker, marker_at = frame_id, position
            key = (parent + 1) << 32 | frame_id
            node = trie.get(key)
            if node is None:
                node = trie[key] = self.node_count
                self.node_count += 1
                self.new_nodes.append((parent, frame_id))
            nodes.append(node)
            parent = node
        self.previous[thread_id] = (frames, nodes, marker, marker_at)
        return parent, marker

    def add(self, main_thread, others, weight: int) -> None:
        self.samples += weight
        frames = main_thread.frame_info
        if not frames:
            return
        leaf, marker = self.insert(main_thread.thread_id, frames)
        self.main[leaf] = self.main.get(leaf, 0) + weight
        if marker < 0:
            return
        for thread in others:
            if thread.frame_info:
                leaf, _ = self.insert(thread.thread_id, thread.frame_info)
                key = (marker, thread.thread_id, leaf)
                self.threads[key] = self.threads.get(key, 0) + weight

    def maybe_flush(self) -> None:
        if len(self.main) + len(self.threads) >= FLUSH_ENTRIES or time.monotonic() - self.flushed_at >= FLUSH_SECONDS:
            self.flush()

    def flush(self, **extra) -> None:
        line = {
            "frames": self.new_frames,
            "nodes": self.new_nodes,
            "main": list(self.main.items()),
            "threads": [[*key, weight] for key, weight in self.threads.items()],
            "failed": self.failed,
            "seconds": round(time.perf_counter() - self.started, 3),
            **extra,
        }
        self.file.write(json.dumps(line, separators=(",", ":")) + "\n")
        self.file.flush()
        self.new_frames, self.new_nodes, self.main, self.threads, self.failed = [], [], {}, {}, 0
        self.flushed_at = time.monotonic()
        if len(self.trie) + len(self.by_object) > FORGET_AFTER:
            # The ids written so far stay valid: each thread's next stack can still start from its previous one
            self.frame_ids, self.by_object, self.trie = {}, {}, {}


class Control:
    """The plugin's side of the conversation: which test runs now, and the sampling interval it wants."""

    def __init__(self, path: str | None) -> None:
        self.map = None
        if path:
            with contextlib.suppress(OSError, ValueError), open(path, "rb") as f:
                self.map = mmap.mmap(f.fileno(), CONTROL.size, access=mmap.ACCESS_READ)

    def read(self) -> tuple[int, int]:
        if self.map is None:
            return 0, 0
        return CONTROL.unpack_from(self.map, 0)


def attach(pid: int, interval_usec: int, *, all_threads: bool) -> SampleProfiler:
    # The target allows us to attach with prctl(PR_SET_PTRACER) right after starting us, retry until it has
    for attempt in range(ATTACH_RETRIES):
        try:
            return SampleProfiler(pid, interval_usec, all_threads=all_threads)
        except (PermissionError, SystemExit, RuntimeError, OSError):
            if attempt == ATTACH_RETRIES - 1:
                raise
            time.sleep(0.05)
    raise AssertionError


def main_thread_of(snapshot):
    for interpreter in snapshot:
        for thread in interpreter.threads:
            if thread.status & THREAD_STATUS_MAIN_THREAD:
                return thread
    for interpreter in snapshot:  # older builds don't flag the main thread: it's the one the unwinder returns
        if interpreter.threads:
            return interpreter.threads[-1]
    return None


def gone(pid: int) -> bool:
    """Whether the process exited (or, where there's /proc, is a zombie)."""
    if os.path.isdir("/proc/self"):
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                return f.read().rsplit(b")", 1)[1].split()[0] == b"Z"
        except FileNotFoundError:
            return True
        except (OSError, IndexError):
            pass
    try:  # macOS has no /proc
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return False


def sample(pid: int, main_unwinder, all_unwinder, *, writer: StackWriter, control: Control, unit_usec: int) -> None:
    """Sample until interrupted or the target exits."""
    unit = unit_usec / 1_000_000
    next_time = time.perf_counter()
    test, test_samples = -1, 0
    skip_threads = 0
    failures = 0
    while True:
        sequence, wanted_usec = control.read()
        interval = max(unit, wanted_usec / 1_000_000)
        if sequence != test:
            test, test_samples = sequence, 0
        if sequence and test_samples >= SAMPLES_PER_TEST:
            interval *= SLOW_DOWN
        now = time.perf_counter()
        if next_time > now:
            time.sleep(next_time - now)
            continue
        weight = max(1, round(interval / unit))
        try:
            main_thread = main_thread_of(main_unwinder.get_stack_trace())
            others = []
            idle = main_thread is not None and not main_thread.status & (THREAD_STATUS_HAS_GIL | THREAD_STATUS_ON_CPU)
            if idle and all_unwinder is not None and skip_threads <= 0:
                others = [
                    thread
                    for interpreter in all_unwinder.get_stack_trace()
                    for thread in interpreter.threads
                    if thread.thread_id != main_thread.thread_id
                ]
                if not others:
                    skip_threads = NO_THREADS_BACKOFF
            skip_threads -= 1
            if main_thread is not None:
                writer.add(main_thread, others, weight)
                test_samples += 1
        except ProcessLookupError:
            return
        except (RuntimeError, UnicodeDecodeError, MemoryError, OSError):
            # Failures only count once sampling works again: a process that died (a zombie until its parent
            # reaps it) fails every sample until it's gone
            failures += 1
            if failures % GONE_CHECK == 0 and gone(pid):
                return
        else:
            writer.failed += failures
            failures = 0
        writer.maybe_flush()
        # Don't make up missed samples in a burst: a sample stands for its interval either way
        next_time = max(next_time + interval, time.perf_counter() - interval)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--rate", type=int, default=1000)
    parser.add_argument("--out", required=True)
    parser.add_argument("--ready", required=True)
    parser.add_argument("--control")
    args = parser.parse_args()

    # Let the tests win when they compete with the sampler for a CPU: losing a sample doesn't bias the split
    with contextlib.suppress(OSError, AttributeError):
        os.nice(10)
    interval_usec = max(1, 1_000_000 // args.rate)

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        main_profiler = attach(args.pid, interval_usec, all_threads=False)
    except BaseException as e:
        Path(args.ready).write_text(json.dumps({"ok": False, "error": f"{type(e).__name__}: {e}"}))
        return 1
    try:
        all_unwinder = attach(args.pid, interval_usec, all_threads=True).unwinder
    except BaseException:
        all_unwinder = None
    writer = StackWriter(Path(args.out))
    Path(args.ready).write_text(json.dumps({"ok": True, "pid": os.getpid()}))
    with contextlib.suppress(KeyboardInterrupt):
        sample(
            args.pid,
            main_profiler.unwinder,
            all_unwinder,
            writer=writer,
            control=Control(args.control),
            unit_usec=interval_usec,
        )
    with contextlib.suppress(KeyboardInterrupt):
        writer.flush(done=True, samples=writer.samples)
    sys.stdout.write(f"sampled {writer.samples} samples in {time.perf_counter() - writer.started:.2f}s\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
