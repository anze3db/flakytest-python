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
