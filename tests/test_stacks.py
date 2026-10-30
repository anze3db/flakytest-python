import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from flakytest.profiling.markers import MARKER_FILENAME
from flakytest.profiling.stacks import read_samples
from tests.helpers import write_tachyon_samples


def paths(samples):
    """Every sampled stack as (frames from the root, weight)."""
    result = []
    for leaf, weight in samples.main.items():
        frames = []
        node = leaf
        while node >= 0:
            parent, frame = samples.nodes[node]
            frames.append(samples.frames[frame])
            node = parent
        result.append((tuple(reversed(frames)), weight))
    return sorted(result)


def test_py_spy_raw_format_keeps_the_main_thread(tmp_path):
    path = tmp_path / "samples-1.txt"
    path.write_text(
        f"thread (0x7F01);<module> (/venv/bin/pytest:8);__flakytest_call__ ({MARKER_FILENAME}:1);"
        "handle (C:\\proj\\app.py:12) 3\n"
        "thread (0x7F02);run (/usr/lib/python3.13/threading.py:1012) 5\n"
        "not a stack line\n"
    )
    samples = read_samples(path)
    marker = (MARKER_FILENAME, "__flakytest_call__", 1)
    assert paths(samples) == [((("/venv/bin/pytest", "<module>", 8), marker, ("C:\\proj\\app.py", "handle", 12)), 3)]
    assert samples.complete


def test_tachyon_format(tmp_path):
    frames = (("/a.py", "main", 1), ("/b.py", "work", 2))
    other = (("/c.py", "pool", 3),)
    path = write_tachyon_samples(
        tmp_path / "samples-1.json", [(frames, 7), (frames[:1], 2)], [(frames[0], 9, other, 4)]
    )
    samples = read_samples(path)
    assert paths(samples) == [(frames[:1], 2), (frames, 7)]
    assert list(samples.threads.items()) == [((0, 9, 2), 4)]
    assert samples.complete


def test_tachyon_file_cut_short_by_a_crash(tmp_path):
    path = write_tachyon_samples(tmp_path / "samples-1.json", [((("/a.py", "main", 1),), 3)], done=False)
    path.write_text(path.read_text() + '{"frames": [["/b.py"')
    samples = read_samples(path)
    assert sum(samples.main.values()) == 3
    assert not samples.complete


def test_tachyon_sampler_starts_over_when_its_stacks_pile_up(tmp_path, monkeypatch):
    tachyon_sampler = pytest.importorskip("flakytest.profiling.tachyon_sampler")  # Python 3.15+
    monkeypatch.setattr(tachyon_sampler, "FORGET_AFTER", 3)
    path = tmp_path / "samples-1.jsonl"
    writer = tachyon_sampler.StackWriter(path)
    root = ("/venv/bin/pytest", 8, "<module>")  # FrameInfo: filename, line, function
    expected = []
    for test in ("a", "b", "c"):
        marker = (MARKER_FILENAME, 1, f"__flakytest_call_{test}__")
        for line in (1, 2):
            leaf = ("/app.py", line, "work")
            writer.add(SimpleNamespace(thread_id=1, frame_info=[leaf, marker, root]), [], 2)
            expected.append(((root[0], root[2], 8), (MARKER_FILENAME, marker[2], 1), ("/app.py", "work", line)))
        writer.flush()
        assert not writer.trie
    writer.file.close()
    samples = read_samples(path)
    assert paths(samples) == sorted((stack, 2) for stack in expected)
    assert len(samples.frames) > len(set(samples.frames))  # it forgot the frames it had written


def test_tachyon_sampler_knows_when_its_process_is_gone_without_proc(monkeypatch):
    tachyon_sampler = pytest.importorskip("flakytest.profiling.tachyon_sampler")  # Python 3.15+
    finished = subprocess.Popen([sys.executable, "-c", "pass"])
    finished.wait()
    monkeypatch.setattr(os.path, "isdir", lambda path: False)  # macOS has no /proc
    assert not tachyon_sampler.gone(os.getpid())
    assert tachyon_sampler.gone(finished.pid)
