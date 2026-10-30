"""Where profiling runs live on disk: <rootdir>/.flakytest/runs/<run id>/profile.json."""

import contextlib
import json
from pathlib import Path
from typing import Any

FLAKYTEST_DIR = ".flakytest"
# Runs that have this share of their tests in common ran the same selection of tests
SAME_SELECTION = 0.9
PREVIOUS_RUNS = 5  # how many earlier runs to look through for one of the same tests, to compare with


def runs_dir(root: Path) -> Path:
    return root / FLAKYTEST_DIR / "runs"


def ignore_in_git(runs: Path) -> None:
    """Keep runs out of version control without asking people to edit their .gitignore."""
    gitignore = runs.parent / ".gitignore"
    if runs.parent.name == FLAKYTEST_DIR and not gitignore.exists():
        gitignore.write_text("# Created by flakytest: profiling runs are local\n*\n")


def mark_latest(run_dir: Path) -> None:
    (run_dir.parent / "latest").write_text(run_dir.name + "\n")


def find_root(start: Path | None = None) -> Path:
    """The closest directory (from start upwards) that has a .flakytest directory, else start."""
    start = (start or Path.cwd()).resolve()
    for directory in (start, *start.parents):
        if (directory / FLAKYTEST_DIR).is_dir():
            return directory
    return start


def list_runs(root: Path) -> list[Path]:
    directory = runs_dir(root)
    if not directory.is_dir():
        return []
    return sorted((p for p in directory.iterdir() if (p / "profile.json").exists()), key=lambda p: p.name)


def unfinished_runs(root: Path) -> list[Path]:
    """Runs that started but never wrote their profile: killed, or hung (pytest-xdist after workers ran out of
    memory) and interrupted."""
    directory = runs_dir(root)
    if not directory.is_dir():
        return []
    return sorted(
        (
            p
            for p in directory.iterdir()
            if p.is_dir() and not (p / "profile.json").exists() and any(p.glob("meta-*.json"))
        ),
        key=lambda p: p.name,
    )


def recorded_tests(run_dir: Path) -> int:
    """How many tests an unfinished run's processes recorded."""
    count = 0
    for path in run_dir.glob("tests-*.jsonl"):
        with path.open("rb") as f:
            count += sum(chunk.count(b"\n") for chunk in iter(lambda: f.read(1 << 20), b""))
    return count


def resolve_run(root: Path, ref: str | None = None) -> Path | None:
    """A run directory from a path, a run id (or its prefix), "latest" or "previous". A run id can name an
    unfinished run."""
    if ref and Path(ref).exists():
        path = Path(ref)
        return path.parent if path.is_file() else path
    runs = list_runs(root)
    if ref not in (None, "", "latest", "previous"):
        matches = [
            run for run in sorted([*runs, *unfinished_runs(root)]) if run.name.startswith(ref) or ref in run.name
        ]
        return matches[-1] if matches else None
    if not runs:
        return None
    if ref in (None, "", "latest"):
        latest = runs_dir(root) / "latest"
        if latest.exists():
            candidate = runs_dir(root) / latest.read_text().strip()
            if (candidate / "profile.json").exists():
                return candidate
        return runs[-1]
    if ref == "previous":
        current = resolve_run(root, "latest")
        earlier = [run for run in runs if current is None or run.name < current.name]
        return earlier[-1] if earlier else None
    return None


def load_profile(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "profile.json").read_text())


SUMMARY_FILE = "summary.json"


def write_summary(profile: dict[str, Any], run_dir: Path) -> None:
    """What comparing with a later run needs, so it doesn't have to load the whole profile."""
    summary = {
        "run": {key: profile["run"].get(key) for key in ("id", "started_at", "duration", "exit_status", "workers")},
        "tests": [
            {"nodeid": test["nodeid"], "duration": test["duration"], "outcome": test["outcome"]}
            for test in profile["tests"]
        ],
    }
    (run_dir / SUMMARY_FILE).write_text(json.dumps(summary, separators=(",", ":")))


def same_selection(old: dict[str, Any], new: dict[str, Any]) -> bool:
    old_ids = {t["nodeid"] for t in old["tests"]}
    new_ids = {t["nodeid"] for t in new["tests"]}
    return len(old_ids & new_ids) >= SAME_SELECTION * max(len(old_ids), len(new_ids), 1)


def previous_summary(run_dir: Path, profile: dict[str, Any]) -> dict[str, Any] | None:
    """The summary of the latest earlier run of the same tests, else of the run before (a full run after a few
    tests, or the other way round, says little about speed)."""
    earlier = sorted(
        (run for run in run_dir.parent.iterdir() if run.name < run_dir.name and (run / "profile.json").exists()),
        reverse=True,
    )
    previous = None
    for run in earlier[:PREVIOUS_RUNS]:
        with contextlib.suppress(OSError, ValueError, KeyError):
            summary = load_summary(run)
            if previous is None:
                previous = summary
            if same_selection(summary, profile):
                return summary
    return previous


def load_summary(run_dir: Path) -> dict[str, Any]:
    """A run's summary.json; runs from before it existed fall back to their profile."""
    path = run_dir / SUMMARY_FILE
    if path.exists():
        return json.loads(path.read_text())
    return load_profile(run_dir)
