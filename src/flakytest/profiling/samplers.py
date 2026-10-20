"""Start and stop a sampling profiler attached to the current process: tachyon on 3.15+, else py-spy."""

import contextlib
import ctypes
import importlib.util
import json
import mmap
import os
import shutil
import signal
import struct
import subprocess
import sys
import time
from pathlib import Path

PR_SET_PTRACER = 0x59616D61
START_TIMEOUT = 15.0
STOP_TIMEOUT = 120.0
BACKENDS = ("auto", "tachyon", "py-spy", "none")
DEFAULT_RATES = {"tachyon": 1000, "py-spy": 500}
# The control file tachyon_sampler reads: test sequence number, sampling interval in microseconds
CONTROL = struct.Struct("<QI")


def tachyon_available() -> bool:
    if sys.version_info < (3, 15):
        return False
    try:
        return importlib.util.find_spec("profiling.sampling") is not None
    except ImportError:
        return False


def find_py_spy() -> str | None:
    beside_python = Path(sys.executable).parent / ("py-spy.exe" if os.name == "nt" else "py-spy")
    if beside_python.exists():
        return str(beside_python)
    return shutil.which("py-spy")


def choose_backend(requested: str) -> tuple[str, str | None]:
    """The backend to use and, when it is "none", why."""
    if requested == "none":
        return "none", "profiling disabled with --flakytest-backend none"
    if requested in ("auto", "tachyon") and tachyon_available():
        return "tachyon", None
    if requested == "tachyon":
        return "none", f"tachyon needs Python 3.15+ (this is {sys.version.split()[0]})"
    if find_py_spy():
        return "py-spy", None
    if sys.version_info >= (3, 15):
        return "none", "py-spy is not installed and tachyon (profiling.sampling) is unavailable"
    return "none", (
        f"no sampling profiler for Python {sys.version.split()[0]}: install py-spy "
        "(pip install 'flakytest[py-spy]') or run on Python 3.15+ to use tachyon"
    )


def allow_tracing_by(pid: int) -> None:
    """Let a child attach to us even with Yama ptrace_scope=1 (the default on Ubuntu and GitHub Actions)."""
    if not sys.platform.startswith("linux"):
        return
    with contextlib.suppress(OSError, AttributeError):
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PTRACER, ctypes.c_ulong(pid), 0, 0, 0)


class Sampler:
    def __init__(self, backend: str, rate: int, out_dir: Path) -> None:
        self.backend = backend
        self.rate = rate
        self.pid = os.getpid()
        self.out = out_dir / f"samples-{self.pid}.{'jsonl' if backend == 'tachyon' else 'txt'}"
        self.control_path = out_dir / f"control-{self.pid}"
        self.control: mmap.mmap | None = None
        self.test_sequence = 0
        self.sampling_rate = rate
        self.log = out_dir / f"sampler-{self.pid}.log"
        self.process: subprocess.Popen[bytes] | None = None
        self.error: str | None = None
        self.started_at = 0.0
        self.stopped_at = 0.0

    def start(self) -> bool:
        ready = self.out.with_suffix(".ready")
        if self.backend == "tachyon":
            self._open_control()
            command = [
                sys.executable,
                "-m",
                "flakytest.profiling.tachyon_sampler",
                "--pid",
                str(self.pid),
                "--rate",
                str(self.rate),
                "--out",
                str(self.out),
                "--ready",
                str(ready),
                "--control",
                str(self.control_path),
            ]
        else:
            command = [
                find_py_spy() or "py-spy",
                "record",
                "--pid",
                str(self.pid),
                "--rate",
                str(self.rate),
                "--format",
                "raw",
                "--threads",
                "--idle",
                "--full-filenames",
                "--nonblocking",
                "--output",
                str(self.out),
            ]
        with self.log.open("wb") as log:
            try:
                # A new session keeps Ctrl+C from killing the profiler before it writes its samples
                self.process = subprocess.Popen(command, stdout=log, stderr=log, start_new_session=True)
            except OSError as e:
                self.error = f"could not start {self.backend}: {e}"
                return False
        allow_tracing_by(self.process.pid)
        # Let the tests win when they compete with the profiler for a CPU (tachyon_sampler renices itself)
        if self.backend == "py-spy" and hasattr(os, "setpriority"):
            with contextlib.suppress(OSError):
                os.setpriority(os.PRIO_PROCESS, self.process.pid, 10)

        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if self.backend == "tachyon" and ready.exists():
                with contextlib.suppress(ValueError):
                    status = json.loads(ready.read_text())
                    if status.get("ok"):
                        break
                    self.error = f"tachyon could not attach: {status.get('error')}"
                    return False
            if self.backend == "py-spy" and "Sampling process" in self.log.read_text(errors="replace"):
                break
            if self.process.poll() is not None:
                self.error = f"{self.backend} exited: {self._log_tail()}"
                return False
            time.sleep(0.01)
        else:
            self.error = f"{self.backend} did not start within {START_TIMEOUT:.0f}s"
            self.process.kill()
            return False
        self.started_at = time.perf_counter()
        return True

    def _open_control(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self.control_path.write_bytes(bytes(CONTROL.size))
            with self.control_path.open("r+b") as f:
                self.control = mmap.mmap(f.fileno(), CONTROL.size)

    def next_test(self) -> None:
        """Tell the sampler a new test started (tachyon samples a long test less often after a while)."""
        if self.control is not None:
            self.test_sequence += 1
            struct.pack_into("<Q", self.control, 0, self.test_sequence)

    def set_rate(self, rate: int) -> None:
        """Sample less often from now on (tachyon only; never faster than the rate it started with)."""
        if self.control is not None and 0 < rate < self.rate:
            self.sampling_rate = rate
            struct.pack_into("<I", self.control, 8, 1_000_000 // rate)

    def stop(self) -> Path | None:
        if self.process is None:
            return None
        self.stopped_at = time.perf_counter()
        if self.process.poll() is None:
            self.process.send_signal(signal.SIGINT)
        try:
            self.process.wait(timeout=STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.error = f"{self.backend} did not finish writing samples within {STOP_TIMEOUT:.0f}s"
            return None
        if not self.out.exists():
            self.error = f"{self.backend} wrote no samples: {self._log_tail()}"
            return None
        return self.out

    def _log_tail(self) -> str:
        try:
            lines = [line for line in self.log.read_text(errors="replace").splitlines() if line.strip()]
        except OSError:
            return "no output"
        return " | ".join(lines[-3:]) or "no output"
