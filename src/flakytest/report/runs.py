"""Where profiling runs live on disk: <rootdir>/.flakytest/runs/<run id>/profile.json."""

import json
from pathlib import Path
from typing import Any

FLAKYTEST_DIR = ".flakytest"


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


def resolve_run(root: Path, ref: str | None = None) -> Path | None:
    """A run directory from a path, a run id (or its prefix), "latest" or "previous"."""
    if ref and Path(ref).exists():
        path = Path(ref)
        return path.parent if path.is_file() else path
    runs = list_runs(root)
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
    matches = [run for run in runs if run.name.startswith(ref) or ref in run.name]
    return matches[-1] if matches else None


def load_profile(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "profile.json").read_text())
