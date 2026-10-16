"""A minimal MCP server over stdio so coding agents can ask about local profiling runs.

Add it to Claude Code with `claude mcp add flakytest -- flakytest mcp` (run from the project root).
"""

import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from flakytest.__about__ import __version__
from flakytest.report import runs, text

PROTOCOL_VERSION = "2025-06-18"
RUN_PROPERTY = {"type": "string", "description": "Run id or prefix, 'latest' (default) or 'previous'."}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_runs",
        "description": "List the local profiling runs (newest last) with a one-line summary each.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_report",
        "description": "The full Markdown report of a run: where test time goes, ranked fix-next suggestions "
        "(with code locations and how to keep tests correct), slowest tests and fixtures, warnings.",
        "inputSchema": {"type": "object", "properties": {"run": RUN_PROPERTY, "top": {"type": "integer"}}},
    },
    {
        "name": "get_test",
        "description": "One test (exact node id) or a group (file or class prefix): phases, categories, pieces of "
        "work, hot lines in project code with source, fixtures, queries, the call tree and related fixes.",
        "inputSchema": {
            "type": "object",
            "properties": {"nodeid": {"type": "string"}, "run": RUN_PROPERTY},
            "required": ["nodeid"],
        },
    },
    {
        "name": "list_slow_tests",
        "description": "The slowest tests with a short explanation of why each is slow.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run": RUN_PROPERTY,
                "limit": {"type": "integer"},
                "filter": {"type": "string", "description": "Only tests whose node id contains this."},
            },
        },
    },
    {
        "name": "compare_runs",
        "description": "Compare two runs: tests that got faster/slower, and correctness checks (tests whose "
        "outcome changed or that didn't run). Use after changing tests to confirm nothing was lost.",
        "inputSchema": {
            "type": "object",
            "properties": {"old": RUN_PROPERTY, "new": RUN_PROPERTY},
        },
    },
]


class Tools:
    def __init__(self, root: Path) -> None:
        self.root = root

    def profile(self, ref: str | None) -> dict[str, Any]:
        run_dir = runs.resolve_run(self.root, ref or "latest")
        if run_dir is None:
            msg = f"No profiling run {ref or 'latest'!r}. Run the tests with `flakytest run` first."
            raise LookupError(msg)
        return runs.load_profile(run_dir)

    def list_runs(self) -> str:
        found = runs.list_runs(self.root)
        if not found:
            return "No runs yet. Run the tests with `flakytest run` (or `pytest --flakytest-profile`)."
        return "\n".join(f"{p.name}: {text.headline(runs.load_profile(p))}" for p in found)

    def get_report(self, run: str | None = None, top: int = 15) -> str:
        return text.markdown_report(self.profile(run), top=top)

    def get_test(self, nodeid: str, run: str | None = None) -> str:
        return text.test_report(self.profile(run), nodeid)

    def list_slow_tests(self, run: str | None = None, limit: int = 20, filter: str = "") -> str:  # noqa: A002
        profile = self.profile(run)
        tests = [t for t in profile["tests"] if filter in t["nodeid"]]
        rows = sorted(tests, key=lambda t: -t["duration"])[:limit]
        lines = ["| Test | Time | Outcome | Why |", "|---|---:|---|---|"]
        lines += [f"| `{t['nodeid']}` | {text.fmt(t['duration'])} | {t['outcome']} | {text.why(t)} |" for t in rows]
        return "\n".join(lines)

    def compare_runs(self, old: str | None = None, new: str | None = None) -> str:
        return text.compare_report(self.profile(old or "previous"), self.profile(new or "latest"))


def handle(tools: Tools, message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if request_id is None:  # notifications need no answer
        return None
    if method == "initialize":
        result: Any = {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "flakytest", "version": __version__},
            "instructions": "Profiles of this project's test suite. Start with get_report, drill into tests with "
            "get_test, and use compare_runs after a change to check speed and that no test was lost.",
        }
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = message.get("params") or {}
        handler: Callable[..., str] | None = getattr(tools, params.get("name", ""), None)
        if handler is None or params.get("name") not in {t["name"] for t in TOOLS}:
            return _error(request_id, -32602, f"Unknown tool {params.get('name')!r}")
        try:
            output = handler(**(params.get("arguments") or {}))
            result = {"content": [{"type": "text", "text": output}], "isError": False}
        except (LookupError, TypeError) as e:
            result = {"content": [{"type": "text", "text": str(e)}], "isError": True}
    else:
        return _error(request_id, -32601, f"Method not found: {method}")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve(root: Path, stdin: Any = None, stdout: Any = None) -> None:
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout
    tools = Tools(root)
    for line in stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response: dict[str, Any] | None = _error(None, -32700, "Parse error")
        else:
            response = handle(tools, message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()
