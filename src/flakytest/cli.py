"""The `flakytest` command: run tests under the profiler and look at the results."""

import argparse
import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any

from flakytest import cloud, mcp
from flakytest.__about__ import __version__
from flakytest.profiling.samplers import BACKENDS, choose_backend, find_py_spy, tachyon_available
from flakytest.report import runs, text

DEFAULT_PORT = 8765


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="flakytest",
        description="See where your test suite spends its time, and what to fix first.",
    )
    parser.add_argument("--version", action="version", version=f"flakytest {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="<command>")

    run = commands.add_parser("run", help="run pytest with profiling, then show the report")
    run.add_argument("--rate", type=int, help="samples per second (default 1000 tachyon / 500 py-spy)")
    run.add_argument("--backend", choices=BACKENDS, default="auto")
    open_group = run.add_mutually_exclusive_group()
    open_group.add_argument("--open", dest="open", action="store_true", default=None, help="open the report")
    open_group.add_argument("--no-open", dest="open", action="store_false", help="don't open the report")
    run.add_argument("pytest_args", nargs=argparse.REMAINDER, help="arguments for pytest (after --)")

    report = commands.add_parser("report", help="print a run's report (Markdown, for people and agents)")
    report.add_argument("run", nargs="?", default="latest", help="run id, path, latest (default) or previous")
    report.add_argument("--format", choices=("markdown", "json"), default="markdown")
    report.add_argument("--test", help="one test, or a file/class prefix, with its call tree and hot lines")
    report.add_argument("--top", type=int, default=15, help="how many slow tests to list")

    compare = commands.add_parser("compare", help="compare two runs: speed and correctness")
    compare.add_argument("old", nargs="?", default="previous")
    compare.add_argument("new", nargs="?", default="latest")

    serve = commands.add_parser("serve", help="serve the reports on localhost and open the latest")
    serve.add_argument("run", nargs="?", default="latest")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--no-open", dest="open", action="store_false", default=True)

    commands.add_parser("list", help="list profiling runs")

    upload = commands.add_parser("upload", help="upload a run to flakytest.dev (needs FLAKYTEST_SECRET_TOKEN)")
    upload.add_argument("run", nargs="?", default="latest")

    commands.add_parser("mcp", help="MCP server (stdio) so coding agents can query your local runs")
    commands.add_parser("doctor", help="check that profiling works here")

    args = parser.parse_args(argv)
    handlers = {
        "run": cmd_run,
        "report": cmd_report,
        "compare": cmd_compare,
        "serve": cmd_serve,
        "list": cmd_list,
        "upload": cmd_upload,
        "mcp": cmd_mcp,
        "doctor": cmd_doctor,
    }
    if args.command is None:
        parser.print_help()
        return 0
    return handlers[args.command](args)


def root() -> Path:
    return runs.find_root()


def load(ref: str) -> tuple[Path, dict[str, Any]] | None:
    run_dir = runs.resolve_run(root(), ref)
    if run_dir is None:
        sys.stderr.write(
            f"No profiling run found for {ref!r} under {runs.runs_dir(root())}.\n"
            "Run your tests with `flakytest run` (or `pytest --flakytest-profile`) first.\n"
        )
        return None
    return run_dir, runs.load_profile(run_dir)


def cmd_run(args: argparse.Namespace) -> int:
    pytest_args = args.pytest_args[1:] if args.pytest_args[:1] == ["--"] else args.pytest_args
    command = [sys.executable, "-m", "pytest", "--flakytest-profile", f"--flakytest-backend={args.backend}"]
    if args.rate:
        command.append(f"--flakytest-rate={args.rate}")
    command += pytest_args
    before = {p.name for p in runs.list_runs(root())}
    exit_code = subprocess.call(command)
    new_runs = [p for p in runs.list_runs(root()) if p.name not in before]
    if not new_runs:
        return exit_code
    report = new_runs[-1] / "index.html"
    interactive = sys.stdout.isatty() and not os.environ.get("CI")
    if args.open or (args.open is None and interactive):
        webbrowser.open(report.resolve().as_uri())
    return exit_code


def cmd_report(args: argparse.Namespace) -> int:
    loaded = load(args.run)
    if loaded is None:
        return 1
    _, profile = loaded
    if args.format == "json":
        json.dump(agent_summary(profile, args.top, args.test), sys.stdout, indent=1)
        sys.stdout.write("\n")
    elif args.test:
        sys.stdout.write(text.test_report(profile, args.test))
    else:
        sys.stdout.write(text.markdown_report(profile, top=args.top))
    return 0


def agent_summary(profile: dict[str, Any], top: int = 15, query: str | None = None) -> dict[str, Any]:
    """The profile without its raw stacks: what an agent needs to decide what to change."""
    tests = text.find_tests(profile, query) if query else profile["tests"]
    slowest = sorted(tests, key=lambda t: -t["duration"])[:top]
    frames = profile["frames"]
    sources = profile.get("sources", {})

    def line(entry: dict[str, Any]) -> dict[str, Any]:
        path, func, lineno, _ = frames[entry["frame"]]
        return {
            "location": f"{path}:{lineno}",
            "function": func,
            "code": sources.get(str(entry["frame"]), ""),
            "seconds": entry["seconds"],
            "categories": entry.get("categories", {}),
        }

    return {
        "run": {
            k: profile["run"][k] for k in ("id", "started_at", "duration", "exit_status", "environment", "profiler")
        },
        "summary": profile["summary"],
        "insights": profile["insights"],
        "warnings": profile["warnings"],
        "slowest_tests": [
            {
                "nodeid": t["nodeid"],
                "outcome": t["outcome"],
                "duration": t["duration"],
                "phases": t["phases"],
                "why": text.why(t),
                "categories": t["categories"],
                "segments": t["segments"][:5],
                "hot_lines": [line(h) for h in t["hot_lines"][:5]],
                "fixtures": sorted(t["fixtures"], key=lambda f: -f["duration"])[:5],
                "db": t.get("db"),
            }
            for t in slowest
        ],
        "fixtures": profile.get("fixtures", [])[:top],
    }


def cmd_compare(args: argparse.Namespace) -> int:
    old = load(args.old)
    new = load(args.new)
    if old is None or new is None:
        return 1
    sys.stdout.write(text.compare_report(old[1], new[1]))
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    found = runs.list_runs(root())
    if not found:
        sys.stdout.write("No runs yet. Try `flakytest run`.\n")
        return 0
    latest = runs.resolve_run(root(), "latest")
    for run_dir in found:
        profile = runs.load_profile(run_dir)
        mark = "*" if run_dir == latest else " "
        sys.stdout.write(f"{mark} {run_dir.name}  {text.headline(profile)}\n")
    return 0


class QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass


def cmd_serve(args: argparse.Namespace) -> int:
    loaded = load(args.run)
    if loaded is None:
        return 1
    run_dir, _ = loaded
    directory = runs.runs_dir(root())
    handler = functools.partial(QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    url = f"http://127.0.0.1:{server.server_address[1]}/{run_dir.name}/index.html"
    sys.stdout.write(f"Serving {directory} at {url} (Ctrl+C to stop)\n")
    if args.open:
        threading.Timer(0.3, webbrowser.open, (url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_upload(args: argparse.Namespace) -> int:
    loaded = load(args.run)
    if loaded is None:
        return 1
    if not cloud.token:
        sys.stderr.write("Set FLAKYTEST_SECRET_TOKEN (from your project on https://flakytest.dev) to upload.\n")
        return 1
    url = cloud.upload_profile(loaded[1])
    if not url:
        sys.stderr.write("Upload failed.\n")
        return 1
    sys.stdout.write(f"Uploaded: {url}\n")
    return 0


def cmd_mcp(_: argparse.Namespace) -> int:
    mcp.serve(root())
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    backend, reason = choose_backend("auto")
    lines = [
        f"flakytest {__version__} on Python {sys.version.split()[0]} ({sys.executable})",
        f"tachyon (Python 3.15+ profiling.sampling): {'available' if tachyon_available() else 'not available'}",
        f"py-spy: {find_py_spy() or 'not installed (pip install py-spy)'}",
        f"Profiler that will be used: {backend}" + (f" ({reason})" if reason else ""),
    ]
    if sys.platform.startswith("linux"):
        scope = Path("/proc/sys/kernel/yama/ptrace_scope")
        if scope.exists():
            value = scope.read_text().strip()
            note = {
                "0": "any process can attach",
                "1": "OK: flakytest allows its profiler to attach with prctl(PR_SET_PTRACER)",
                "2": "only root can attach: run with sudo or set it to 1",
                "3": "attaching is disabled: profiling won't work",
            }
            lines.append(f"ptrace_scope={value}: {note.get(value, 'unknown')}")
    elif sys.platform == "darwin":
        lines.append("macOS: sampling profilers need root, run `sudo -E flakytest run` if attaching fails")
    lines.append(f"Runs directory: {runs.runs_dir(root())}")
    lines.append(
        f"Cloud upload: {'on (FLAKYTEST_SECRET_TOKEN is set)' if cloud.token else 'off (no FLAKYTEST_SECRET_TOKEN)'}"
    )
    sys.stdout.write("\n".join(lines) + "\n")
    return 0 if backend != "none" else 1


if __name__ == "__main__":
    sys.exit(main())
