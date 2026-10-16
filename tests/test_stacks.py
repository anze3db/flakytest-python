from flakytest.profiling.stacks import read_samples
from tests.helpers import write_tachyon_samples


def test_py_spy_raw_format(tmp_path):
    path = tmp_path / "samples-1.txt"
    path.write_text(
        "thread (0x7F01);<module> (/venv/bin/pytest:8);handle (C:\\proj\\app.py:12) 3\n"
        "thread (0x7F02);run (/usr/lib/python3.13/threading.py:1012) 5\n"
        "not a stack line\n"
    )
    stacks = read_samples(path)
    assert stacks == [
        ("0x7F01", (("/venv/bin/pytest", "<module>", 8), ("C:\\proj\\app.py", "handle", 12)), 3),
        ("0x7F02", (("/usr/lib/python3.13/threading.py", "run", 1012),), 5),
    ]


def test_tachyon_format(tmp_path):
    frames = (("/a.py", "main", 1), ("/b.py", "work", 2))
    path = write_tachyon_samples(tmp_path / "samples-1.json", [(frames, 7)])
    assert read_samples(path) == [("1", frames, 7)]
