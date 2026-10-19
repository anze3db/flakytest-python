import io
import json

import pytest

from flakytest import cli, mcp
from flakytest.profiling.analysis import build_profile
from flakytest.report import html, runs, text
from tests.helpers import process, record, run_info


def save_run(root, name, tests):
    run_dir = runs.runs_dir(root) / name
    run_dir.mkdir(parents=True)
    info = run_info(backend="none") | {"id": name}
    profile = build_profile(info, [process(tests)], root, {})
    (run_dir / "profile.json").write_text(json.dumps(profile))
    runs.mark_latest(run_dir)
    return run_dir


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    save_run(
        tmp_path,
        "20261001-000000-aaaaaa",
        [record("tests/test_a.py::test_a", call=2.0), record("tests/test_a.py::test_b", call=1.0)],
    )
    save_run(
        tmp_path,
        "20261002-000000-bbbbbb",
        [record("tests/test_a.py::test_a", call=0.5), record("tests/test_a.py::test_c", call=0.1, outcome="failed")],
    )
    return tmp_path


def test_report_markdown(project, capsys):
    assert cli.main(["report"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("# flakytest profile")
    assert "`tests/test_a.py::test_a`" in out
    assert "## Fix next" in out


def test_report_json_for_agents(project, capsys):
    assert cli.main(["report", "--format", "json", "--top", "1"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert [t["nodeid"] for t in data["slowest_tests"]] == ["tests/test_a.py::test_a"]
    assert "frames" not in data


def test_report_single_test(project, capsys):
    assert cli.main(["report", "--test", "tests/test_a.py::test_c"]) == 0
    assert capsys.readouterr().out.startswith("# tests/test_a.py::test_c")


def test_compare_flags_lost_and_changed_tests(project, capsys):
    assert cli.main(["compare"]) == 0
    out = capsys.readouterr().out
    assert "1 tests from before didn't run" in out
    assert "`tests/test_a.py::test_b`" in out
    assert "| `tests/test_a.py::test_a` | 2.00s | 500ms |" in out


def test_list_and_missing_runs(project, capsys, tmp_path_factory, monkeypatch):
    assert cli.main(["list"]) == 0
    assert "* 20261002-000000-bbbbbb" in capsys.readouterr().out
    monkeypatch.chdir(tmp_path_factory.mktemp("empty"))
    assert cli.main(["report"]) == 1
    assert "No profiling run found" in capsys.readouterr().err


def test_mcp_server(project):
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_slow_tests", "arguments": {"limit": 1}},
        },
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "get_test", "arguments": {"nodeid": "nope"}},
        },
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "rm_rf", "arguments": {}}},
    ]
    stdout = io.StringIO()
    mcp.serve(project, io.StringIO("\n".join(json.dumps(r) for r in requests) + "\n"), stdout)
    responses = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert [r["id"] for r in responses] == [1, 2, 3, 4, 5]
    assert responses[0]["result"]["serverInfo"]["name"] == "flakytest"
    assert {t["name"] for t in responses[1]["result"]["tools"]} >= {"get_report", "get_test", "compare_runs"}
    assert "`tests/test_a.py::test_a`" in responses[2]["result"]["content"][0]["text"]
    assert "No test matches" in responses[3]["result"]["content"][0]["text"]
    assert responses[4]["error"]["code"] == -32602


def test_html_report_loads_the_profile_from_a_script(project, tmp_path):
    profile = runs.load_profile(runs.resolve_run(project, "latest"))
    profile["tests"][0]["longrepr"] = "</script><script>alert(1)</script>"
    page = html.write_report(json.dumps(profile), tmp_path).read_text()
    assert '<script src="profile.js"></script>' in page
    assert "alert(1)" not in page  # the data isn't in the page, so it can't break out of a <script>
    script = (tmp_path / "profile.js").read_text()
    assert script.startswith(html.DATA_PREFIX)
    data = json.loads(script.removeprefix(html.DATA_PREFIX).rstrip().removesuffix(";"))
    assert data["tests"][0]["longrepr"] == "</script><script>alert(1)</script>"


def test_terminal_summary_compares_with_previous_run(project):
    old = runs.load_profile(runs.resolve_run(project, "previous"))
    new = runs.load_profile(runs.resolve_run(project, "latest"))
    line = text.versus(old, new)
    assert line.startswith("vs previous run (20261001-000000-aaaaaa): 2.00s → 500ms (-1.50s, -75%)")
    assert "1 new" in line
