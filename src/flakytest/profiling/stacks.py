"""Read the samples written by tachyon_sampler (JSON lines) and py-spy (raw collapsed stacks) into one shape:
a prefix tree of stacks and a weight per leaf node."""

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from flakytest.profiling.markers import MARKER_FILENAME

RawFrame = tuple[str, str, int]  # (filename, function, line)

PY_SPY_FRAME = re.compile(r"^(?P<func>.*) \((?P<file>.*?)(?::(?P<line>\d+))?\)$")
PY_SPY_THREAD = re.compile(r"^(?:thread|process) \(?(?P<id>[^)]*)\)?")


@dataclass
class Samples:
    frames: list[RawFrame] = field(default_factory=list)
    nodes: list[tuple[int, int]] = field(default_factory=list)  # (parent node or -1, frame), parents first
    main: Counter[int] = field(default_factory=Counter)  # the main thread's samples: leaf node -> weight
    # other threads, sampled while the main thread waited: (main thread's marker frame, thread id, leaf) -> weight
    threads: Counter[tuple[int, int, int]] = field(default_factory=Counter)
    failed: int = 0
    complete: bool = False  # the sampler finished writing (False after a crash)


def read_samples(path: Path) -> Samples:
    if path.suffix == ".jsonl":
        return _read_tachyon(path)
    return _read_py_spy(path)


def _read_tachyon(path: Path) -> Samples:
    samples = Samples()
    with path.open(encoding="utf-8") as f:
        for text in f:
            try:
                line = json.loads(text)
            except ValueError:  # the last line of a sampler that was killed while writing it
                break
            samples.frames.extend((frame[0], frame[1], frame[2]) for frame in line["frames"])
            samples.nodes.extend((parent, frame) for parent, frame in line["nodes"])
            for node, weight in line["main"]:
                samples.main[node] += weight
            for marker, thread, node, weight in line["threads"]:
                samples.threads[(marker, thread, node)] += weight
            samples.failed += line.get("failed", 0)
            samples.complete = samples.complete or bool(line.get("done"))
    return samples


def _read_py_spy(path: Path) -> Samples:
    """py-spy aggregates samples per stack, so other threads can't be tied to the main thread's test: they're left
    out, and the main thread is the one with the most marker frames."""
    stacks: list[tuple[str, list[RawFrame], int]] = []
    with path.open(encoding="utf-8", errors="replace") as f:
        for raw_line in f:
            line = raw_line.rstrip("\n")
            stack_text, _, count_text = line.rpartition(" ")
            if not stack_text or not count_text.isdigit():
                continue
            parts = stack_text.split(";")
            thread = "main"
            if parts and (match := PY_SPY_THREAD.match(parts[0])):
                thread = match.group("id")
                parts = parts[1:]
            frames = []
            for part in parts:
                match = PY_SPY_FRAME.match(part)
                if match:
                    frames.append((match.group("file"), match.group("func"), int(match.group("line") or 0)))
                else:
                    frames.append(("", part, 0))
            stacks.append((thread, frames, int(count_text)))
    marked: Counter[str] = Counter()
    total: Counter[str] = Counter()
    for thread, frames, count in stacks:
        total[thread] += count
        if any(frame[0] == MARKER_FILENAME for frame in frames):
            marked[thread] += count
    main = (marked or total).most_common(1)[0][0] if total else None
    samples = Samples(complete=True)
    frame_ids: dict[RawFrame, int] = {}
    node_ids: dict[tuple[int, int], int] = {}
    for thread, frames, count in stacks:
        if thread != main:
            continue
        node = -1
        for frame in frames:
            frame_id = frame_ids.get(frame)
            if frame_id is None:
                frame_id = frame_ids[frame] = len(samples.frames)
                samples.frames.append(frame)
            key = (node, frame_id)
            child = node_ids.get(key)
            if child is None:
                child = node_ids[key] = len(samples.nodes)
                samples.nodes.append(key)
            node = child
        if node >= 0:
            samples.main[node] += count
    return samples
