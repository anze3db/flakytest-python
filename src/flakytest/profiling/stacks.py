"""Read the samples written by tachyon_sampler (JSON) and py-spy (raw collapsed stacks) into one shape."""

import json
import re
from collections.abc import Iterator
from pathlib import Path

RawFrame = tuple[str, str, int]  # (filename, function, line)
RawStack = tuple[str, tuple[RawFrame, ...], int]  # (thread, frames from the root to the leaf, count)

PY_SPY_FRAME = re.compile(r"^(?P<func>.*) \((?P<file>.*?)(?::(?P<line>\d+))?\)$")
PY_SPY_THREAD = re.compile(r"^(?:thread|process) \(?(?P<id>[^)]*)\)?")


def read_samples(path: Path) -> list[RawStack]:
    if path.suffix == ".json":
        return list(_read_tachyon(path))
    return list(_read_py_spy(path))


def _read_tachyon(path: Path) -> Iterator[RawStack]:
    data = json.loads(path.read_text())
    frames = [(f[0], f[1], f[2]) for f in data["frames"]]
    for tid, ids, count in data["stacks"]:
        yield str(tid), tuple(frames[i] for i in ids), count


def _read_py_spy(path: Path) -> Iterator[RawStack]:
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
            yield thread, tuple(frames), int(count_text)
