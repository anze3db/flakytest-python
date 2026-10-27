"""Plain text and Markdown views of a profile: the terminal summary, and reports written for people and
coding agents (`flakytest report`)."""

import contextlib
from collections import Counter
from pathlib import Path
from typing import Any

from flakytest.profiling.insights import own_time
from flakytest.profiling.memory import mb
from flakytest.report.tree import build_tree, fmt, hot_functions, render_tree

CATEGORY_LABELS = {
    "gc": "garbage collection",
    "imports": "imports",
    "password_hashing": "password hashing",
    "migrations": "migrations",
    "db_schema": "DB schema / flush",
    "fixture_loading": "fixture loading",
    "browser": "browser",
    "subprocess": "subprocesses",
    "db": "database",
    "cache": "cache",
    "network": "network",
    "sleep": "sleep",
    "waiting": "waiting",
    "template": "templates",
    "factories": "test data factories",
    "mocking": "mocking",
    "copying": "deepcopy",
    "serialization": "serialization",
    "logging": "logging",
    "test_code": "test code",
    "app_code": "app code",
    "pytest": "pytest",
    "library": "libraries",
    "unsampled": "not sampled",
}
SEVERITY_MARK = {"high": "▲", "medium": "■", "low": "·"}


def label(category: str) -> str:
    return CATEGORY_LABELS.get(category, category.replace("_", " "))


def pct(part: float, total: float) -> str:
    return f"{part / total:.0%}" if total else "-"


def top_category(test: dict[str, Any]) -> tuple[str, float]:
    categories = {k: v for k, v in test["categories"].items() if k != "unsampled"}
    if not categories:
        return "unsampled", test["duration"]
    return max(categories.items(), key=lambda kv: kv[1])


def why(test: dict[str, Any]) -> str:
    """One line: what a test's time is mostly spent on."""
    category, seconds = top_category(test)
    parts = [f"{label(category)} {pct(seconds, test['duration'])}"]
    if test["segments"]:
        segment = test["segments"][0]
        parts.append(f"{segment['label']} in {segment['phase']} {fmt(segment['seconds'])}")
    if test.get("shared_fixture_time", 0) >= 0.05:
        parts.append(f"incl. {fmt(test['shared_fixture_time'])} in shared fixtures")
    db = test.get("db") or {}
    if db.get("queries"):
        parts.append(f"{db['queries']} queries")
    if threads := test.get("threads"):
        work = ", ".join(f"{label(k)} {pct(v, threads['seconds'])}" for k, v in list(threads["categories"].items())[:2])
        parts.append(f"{fmt(threads['seconds'])} in {plural(threads['count'], 'background thread')}: {work}")
    if one_off := one_off_note(test):
        parts.append(one_off)
    return ", ".join(parts)


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def one_off_note(test: dict[str, Any]) -> str:
    """ "+4.6s one-off: first mock_aws_services call in gw2", for a test that paid for warming something up."""
    warmup = test.get("warmup")
    if not warmup or warmup["seconds"] < 0.05:
        return ""
    return f"+{fmt(warmup['seconds'])} one-off: " + ", ".join(cause["what"] for cause in warmup["causes"])


def slowest(tests: list[dict[str, Any]], top: int) -> list[dict[str, Any]]:
    """The slowest tests by their own time: without shared fixtures and one-off warm-up, which any test that
    happens to run first would pay."""
    return sorted(tests, key=own_time, reverse=True)[:top]


def headline(profile: dict[str, Any]) -> str:
    run, summary = profile["run"], profile["summary"]
    profiler = run["profiler"]
    sampling = (
        f"{profiler['backend']} @ {profiler['rate']} Hz, {profiler['samples']:,} samples"
        if profiler["backend"] != "none"
        else "no sampling profiler"
    )
    return (
        f"{summary['tests']} tests in {fmt(run['duration'])} (test time {fmt(summary['test_time'])}) · "
        f"{run['environment']['name']} · Python {run['python']['version']} · {sampling}"
    )


def versus(old: dict[str, Any], new: dict[str, Any]) -> str:
    """One line comparing a run with the previous one: speed of the tests both ran, and lost tests."""
    old_tests = {t["nodeid"]: t for t in old["tests"]}
    common = [t for t in new["tests"] if t["nodeid"] in old_tests]
    if not common:
        return ""
    before = sum(old_tests[t["nodeid"]]["duration"] for t in common)
    after = sum(t["duration"] for t in common)
    parts = [f"vs previous run ({old['run']['id']}): {fmt(before)} → {fmt(after)} ({_delta(after - before, before)})"]
    changed = sum(1 for t in common if t["outcome"] != old_tests[t["nodeid"]]["outcome"])
    if changed:
        parts.append(f"{changed} changed outcome")
    if len(common) < len(new["tests"]):
        parts.append(f"{len(new['tests']) - len(common)} new")
    return ", ".join(parts) + " · flakytest compare"


def warms_up(test: dict[str, Any]) -> bool:
    """A test whose memory includes warming up: the first in its process or class, or one that set up a fixture
    other tests share (whose memory is the fixture's)."""
    return bool((test.get("memory") or {}).get("warmup")) or any(f["scope"] != "function" for f in test["fixtures"])


def memory_hogs(profile: dict[str, Any], top: int) -> list[dict[str, Any]]:
    """The tests that needed the most memory: their peak above the memory the process had when they started. Tests
    that warmed something up are left out: what they needed says more about the order tests ran in."""
    measured = [t for t in profile["tests"] if (t.get("memory") or {}).get("peak_increase") and not warms_up(t)]
    return sorted(measured, key=lambda t: -t["memory"]["peak_increase"])[:top]


def fixture_memory(profile: dict[str, Any], top: int) -> list[tuple[str, str, int]]:
    """Class/module/session fixtures by the private memory they kept."""
    kept: dict[tuple[str, str], int] = {}
    for test in profile["tests"]:
        for fixture in test["fixtures"]:
            if fixture.get("memory"):
                key = (fixture["name"], fixture["scope"])
                kept[key] = kept.get(key, 0) + fixture["memory"]
    return [(name, scope, size) for (name, scope), size in sorted(kept.items(), key=lambda kv: -kv[1])[:top]]


def memory_line(profile: dict[str, Any]) -> str | None:
    memory = profile["summary"].get("memory")
    if not memory or not memory.get("peak"):
        return None
    parts = [f"peak {mb(memory['peak'])}" + (" per process" if len(memory["processes"]) > 1 else "")]
    if len(memory["processes"]) > 1 and memory.get("machine_peak"):
        parts.append(f"{mb(memory['machine_peak'])} across {len(memory['processes'])} workers at once")
    hogs = memory_hogs(profile, 1)
    if hogs:
        parts.append(f"most: {hogs[0]['nodeid']} needed {mb(hogs[0]['memory']['peak_increase'])}")
    return "Memory: " + " · ".join(parts)


def memory_markdown(profile: dict[str, Any], top: int) -> list[str]:
    memory = profile["summary"].get("memory")
    if not memory or not memory.get("peak"):
        return []
    out = ["", "## Memory", ""]
    workers = len(memory["processes"])
    line = f"The test process peaked at {mb(memory['peak'])}"
    if workers > 1:
        line = f"The busiest of {workers} workers peaked at {mb(memory['peak'])}"
        if memory.get("machine_peak"):
            what = "private memory" if memory.get("private") else "memory"
            line += f"; together their {what} peaked at about {mb(memory['machine_peak'])}"
    out.append(line + ".")
    if not memory.get("peak_per_test"):
        out.append(
            "The peak can't be reset per test on this platform, so a test's peak is only known when it raised the "
            "process's peak; the others show no peak."
        )
    hogs = memory_hogs(profile, top)
    if hogs:
        out += ["", "| Test | Needed (peak above start) | Kept after the test | Worker |", "|---|---:|---:|---|"]
        for test in hogs:
            used = test["memory"]
            kept = mb(used["growth"]) if used.get("growth") is not None else "?"
            out.append(f"| `{test['nodeid']}` | {mb(used['peak_increase'])} | {kept} | {test.get('worker') or ''} |")
        out.append(
            "\nLeft out: the first test in each process and class, and tests that set up a class, module or session "
            "fixture, since they pay for warming up."
        )
    shared = [entry for entry in fixture_memory(profile, top) if entry[2] >= 1024**2]
    if shared:
        out += ["", "| Shared fixture | Scope | Kept |", "|---|---|---:|"]
        out += [f"| `{name}` | {scope} | {mb(size)} |" for name, scope, size in shared]
    if workers > 1:
        out += ["", "| Worker | Tests | Peak | Grew by |", "|---|---:|---:|---:|"]
        for process in memory["processes"]:
            peak = mb(process["peak"]) if process["peak"] else "?"
            growth = mb(process["growth"]) if process["growth"] is not None else "?"
            out.append(f"| {process['worker'] or 'main'} | {process['tests']} | {peak} | {growth} |")
    return out


RERUN = {
    "pytest": "flakytest pytest <tests>",
    "django": "flakytest python manage.py test <tests>",
    "unittest": "flakytest python -m unittest <tests>",
}


def terminal_summary(
    profile: dict[str, Any], report_path: Path, upload_url: str | None = None, previous: dict[str, Any] | None = None
) -> list[str]:
    summary = profile["summary"]
    total = summary["test_time"]
    lines = [headline(profile)]
    if previous is not None and (line := versus(previous, profile)):
        lines.append(line)
    categories = [(k, v) for k, v in summary["categories"].items() if v / (total or 1) >= 0.02][:6]
    if categories:
        lines.append("Time goes to: " + " · ".join(f"{label(k)} {pct(v, total)}" for k, v in categories))
    slow = slowest(profile["tests"], 5)
    if slow:
        lines.append("Slowest tests:")
        lines.extend(f"  {fmt(t['duration']):>7}  {t['nodeid']}  ({why(t)})" for t in slow)
    if profile["insights"]:
        lines.append("Fix next:")
        for number, item in enumerate(profile["insights"][:3], 1):
            lines.append(f"  {number}. [{item['severity']}] {item['title']} (~{fmt(item['impact'])} per run)")
    if memory := memory_line(profile):
        lines.append(memory)
    lines.extend(f"⚠ {warning['message']}" for warning in profile["warnings"] if warning["code"] != "short_tests")
    with contextlib.suppress(ValueError):
        report_path = report_path.relative_to(Path.cwd())
    lines.append(f"Report: {report_path}")
    lines.append("  flakytest serve  ·  flakytest report --format markdown  (for agents)")
    if upload_url:
        lines.append(f"Uploaded: {upload_url}")
    return lines


def markdown_report(profile: dict[str, Any], *, top: int = 15) -> str:
    run, summary = profile["run"], profile["summary"]
    total = summary["test_time"] or 1.0
    out = [
        "# flakytest profile",
        "",
        headline(profile),
        "",
        (
            f"Outcomes: {summary['passed']} passed, {summary['failed']} failed, {summary['errors']} errors, "
            f"{summary['skipped']} skipped. Exit status: {run['exit_status']}. Run: `{run['id']}`."
        ),
    ]
    git = run.get("git") or {}
    if git.get("commit_hash"):
        out.append(
            f"Commit: `{git['commit_hash'][:10]}` on `{git.get('branch')}`"
            + (" (uncommitted changes)" if git.get("dirty") else "")
        )

    timeline = summary.get("timeline", {})
    if timeline:
        out += ["", "## Session timeline", ""]
        out.append(" · ".join(f"{name.replace('_', ' ')} {fmt(seconds)}" for name, seconds in timeline.items()))
        for bucket, modules in summary.get("imports", {}).items():
            if modules:
                out.append(
                    f"Slowest imports during {bucket}: "
                    + ", ".join(f"`{m['module']}` {fmt(m['seconds'])}" for m in modules[:5])
                )

    out += ["", "## Where test time goes", "", "| Category | Time | Share |", "|---|---:|---:|"]
    for category, seconds in summary["categories"].items():
        if seconds / total >= 0.005:
            out.append(f"| {label(category)} | {fmt(seconds)} | {pct(seconds, total)} |")
    phases = summary.get("phases", {})
    if phases:
        out.append("")
        out.append(
            "Phases: "
            + " · ".join(f"{phase} {fmt(seconds)} ({pct(seconds, total)})" for phase, seconds in phases.items())
        )

    out += ["", "## Fix next", ""]
    if not profile["insights"]:
        out.append("Nothing stands out. The slowest tests below are the place to look.")
    for number, item in enumerate(profile["insights"], 1):
        out += insight_markdown(profile, item, number)

    out += [
        "",
        f"## Slowest tests (top {top})",
        "",
        "| Test | Time | Setup / call / teardown | Why |",
        "|---|---:|---|---|",
    ]
    for test in slowest(profile["tests"], top):
        p = test["phases"]
        out.append(
            f"| `{test['nodeid']}` | {fmt(test['duration'])} | {fmt(p['setup'])} / {fmt(p['call'])} / "
            f"{fmt(p['teardown'])} | {why(test)} |"
        )

    out += one_off_markdown(profile, top)

    files: Counter[str] = Counter()
    for test in profile["tests"]:
        files[test["file"]] += test["duration"]
    out += ["", "## Slowest files", "", "| File | Time | Tests |", "|---|---:|---:|"]
    counts = Counter(test["file"] for test in profile["tests"])
    for file, seconds in files.most_common(min(top, 10)):
        out.append(f"| `{file}` | {fmt(seconds)} | {counts[file]} |")

    fixtures = [f for f in profile.get("fixtures", []) if f["total"] >= 0.001][:10]
    if fixtures:
        out += ["", "## Slowest fixtures (exact setup time)", "", "| Fixture | Scope | Calls | Total | Mean |"]
        out.append("|---|---|---:|---:|---:|")
        out += [
            f"| `{f['name']}` | {f['scope']} | {f['calls']} | {fmt(f['total'])} | {fmt(f['mean'])} |" for f in fixtures
        ]

    out += memory_markdown(profile, min(top, 10))

    if profile["warnings"]:
        out += ["", "## Warnings", ""]
        out += [f"- {warning['message']}" for warning in profile["warnings"]]

    out += [
        "",
        "## Next steps",
        "",
        "- `flakytest report --test '<nodeid>'` shows one test's call tree, hot lines, fixtures and queries.",
        (
            f"- After a change, rerun the affected tests with `{RERUN[profile['run'].get('runner', 'pytest')]}`, then "
            "`flakytest compare` to check the speed-up and that every test still has the same outcome."
        ),
    ]
    return "\n".join(out) + "\n"


def one_off_markdown(profile: dict[str, Any], top: int) -> list[str]:
    paying = sorted(
        (t for t in profile["tests"] if (t.get("warmup") or {}).get("seconds", 0) >= 0.05),
        key=lambda t: -t["warmup"]["seconds"],
    )
    if not paying:
        return []
    total = sum(t["warmup"]["seconds"] for t in paying)
    out = [
        "",
        "## One-off costs",
        "",
        (
            f"{fmt(total)} in {plural(len(paying), 'test')} goes to warming up: the first call of a fixture in a "
            "process, and imports. Whichever test runs first pays it, so it's left out of the slowest tests above."
        ),
        "",
        "| Test | One-off | What |",
        "|---|---:|---|",
    ]
    for test in paying[:top]:
        what = ", ".join(f"{cause['what']} ({fmt(cause['seconds'])})" for cause in test["warmup"]["causes"])
        out.append(f"| `{test['nodeid']}` | {fmt(test['warmup']['seconds'])} | {what} |")
    return out


def insight_markdown(profile: dict[str, Any], item: dict[str, Any], number: int) -> list[str]:
    out = [
        f"### {number}. {item['title']}",
        "",
        (
            f"{SEVERITY_MARK.get(item['severity'], '')} **{item['severity']}** · "
            f"saves ~{fmt(item['impact'])} per run · `{item['id']}`"
        ),
        "",
        item["summary"],
        "",
        f"**Fix:** {item['fix']}",
    ]
    if item.get("safety"):
        out.append(f"\n**Keep it correct:** {item['safety']}")
    if item.get("locations"):
        out.append("\n**Where (your code):**")
        for location in item["locations"][:5]:
            source = profile.get("sources", {}).get(str(location["frame"]), "")
            code = f" — `{source}`" if source else ""
            out.append(f"- `{location['location']}` in `{location['function']}` ({fmt(location['seconds'])}){code}")
    if item.get("evidence"):
        out.append("\n**Evidence:**")
        out += [f"- {line}" for line in item["evidence"][:5]]
    if item.get("tests"):
        shown = ", ".join(f"`{nodeid}`" for nodeid in item["tests"][:5])
        more = f" and {len(item['tests']) - 5} more" if len(item["tests"]) > 5 else ""
        out.append(f"\n**Tests:** {shown}{more}")
    if item.get("docs"):
        out.append(f"\n**Docs:** {item['docs']}")
    out.append("")
    return out


def find_tests(profile: dict[str, Any], query: str) -> list[dict[str, Any]]:
    """Tests whose node id equals the query, else starts with it (a file or class), else contains it."""
    tests = profile["tests"]
    exact = [t for t in tests if t["nodeid"] == query]
    if exact:
        return exact
    prefix = [t for t in tests if t["nodeid"].startswith(query)]
    return prefix or [t for t in tests if query in t["nodeid"]]


def test_report(profile: dict[str, Any], query: str, *, min_share: float = 0.02) -> str:
    tests = find_tests(profile, query)
    if not tests:
        return f"No test matches `{query}`.\n"
    single = len(tests) == 1
    test = tests[0]
    duration = sum(t["duration"] for t in tests)
    title = test["nodeid"] if single else f"{query} ({len(tests)} tests)"
    out = [f"# {title}", ""]
    if single:
        p = test["phases"]
        samples = sum(test["samples"].values())
        out.append(
            f"{test['outcome']} in {fmt(duration)}: setup {fmt(p['setup'])}, call {fmt(p['call'])}, "
            f"teardown {fmt(p['teardown'])} · {samples} samples"
        )
        if test.get("shared_fixture_time", 0) >= 0.01:
            out.append(
                f"Includes {fmt(test['shared_fixture_time'])} setting up or tearing down class/module/session "
                "fixtures that other tests share."
            )
        used = test.get("memory") or {}
        if used.get("peak_increase") is not None:
            kept = f", kept {mb(used['growth'])} after it" if used.get("growth") is not None else ""
            start = used.get("private_start", used.get("start")) or 0
            what = " of private memory" if "private_start" in used else ""
            note = f" ({used['warmup']}, so it may include warming up)" if used.get("warmup") else ""
            out.append(
                f"Memory: needed {mb(used['peak_increase'])} above the {mb(start)}{what} it started with{kept}{note}."
            )
        if one_off := one_off_note(test):
            out.append(f"One-off: {one_off.split(': ', 1)[1]} ({fmt(test['warmup']['seconds'])}).")
        if threads := test.get("threads"):
            work = ", ".join(f"{label(k)} {fmt(v)}" for k, v in threads["categories"].items())
            out.append(
                f"While it waited, {plural(threads['count'], 'background thread')} worked {fmt(threads['seconds'])}: "
                f"{work}. That time is counted as their work, not as waiting."
            )
        if "undersampled" in test["warnings"] or "low_samples" in test["warnings"]:
            rate = profile["run"]["profiler"]["rate"]
            out.append(f"⚠ Only {samples} samples: rerun with a higher --flakytest-rate (now {rate} Hz) for detail.")
    else:
        out.append(f"{len(tests)} tests, {fmt(duration)} in total.")
        out += ["", "| Test | Time | Why |", "|---|---:|---|"]
        out += [
            f"| `{t['nodeid']}` | {fmt(t['duration'])} | {why(t)} |"
            for t in sorted(tests, key=lambda t: -t["duration"])[:20]
        ]

    categories: Counter[str] = Counter()
    for t in tests:
        categories.update(t["categories"])
    out += ["", "## Where the time goes", "", "| Category | Time | Share |", "|---|---:|---:|"]
    out += [f"| {label(k)} | {fmt(v)} | {pct(v, duration)} |" for k, v in categories.most_common()]

    segments: Counter[tuple[str, str]] = Counter()
    for t in tests:
        for segment in t["segments"]:
            segments[(segment["phase"], segment["label"])] += segment["seconds"]
    if segments:
        out += ["", "## Pieces of work", "", "| Phase | What | Time |", "|---|---|---:|"]
        out += [f"| {phase} | {what} | {fmt(seconds)} |" for (phase, what), seconds in segments.most_common(12)]

    lines: Counter[int] = Counter()
    for t in tests:
        for line in t["hot_lines"]:
            lines[line["frame"]] += line["seconds"]
    if lines:
        out += ["", "## Hot lines in your code", "", "| Line | Function | Time | Code |", "|---|---|---:|---|"]
        for frame, seconds in lines.most_common(12):
            path, func, lineno, _ = profile["frames"][frame]
            source = profile.get("sources", {}).get(str(frame), "")
            out.append(f"| `{path}:{lineno}` | `{func}` | {fmt(seconds)} | `{source}` |")

    functions = hot_functions(profile, tests, 12)
    if functions:
        out += ["", "## Hot functions (self time)", "", "| Function | Where | Self | Total |", "|---|---|---:|---:|"]
        out += [f"| `{f['function']}` | `{f['path']}` | {fmt(f['self'])} | {fmt(f['total'])} |" for f in functions]

    fixtures: Counter[tuple[str, str]] = Counter()
    for t in tests:
        for fixture in t["fixtures"]:
            fixtures[(fixture["name"], fixture["scope"])] += fixture["duration"]
    if fixtures:
        out += ["", "## Fixtures (exact setup time)", "", "| Fixture | Scope | Time |", "|---|---|---:|"]
        out += [f"| `{name}` | {scope} | {fmt(seconds)} |" for (name, scope), seconds in fixtures.most_common(15)]

    queries = [(t, t["db"]) for t in tests if t.get("db")]
    if queries:
        count = sum(db["queries"] for _, db in queries)
        time = sum(db["time"] for _, db in queries)
        out += ["", "## Database", "", f"{count} queries, {fmt(time)}."]
        for t, db in queries[:10]:
            for duplicate in db["duplicates"][:3]:
                out.append(f"- {duplicate['count']}x in `{t['name']}`: `{duplicate['sql']}`")

    root = build_tree(profile, tests)
    if root.seconds:
        out += ["", f"## Call tree (pytest internals hidden, calls under {min_share:.0%} folded)", "", "```"]
        out += render_tree(profile, root, min_share=min_share)
        out.append("```")

    nodeids = {t["nodeid"] for t in tests}
    related = [i for i in profile["insights"] if nodeids & set(i.get("tests", []))]
    if related:
        out += ["", "## Suggestions that apply here", ""]
        for number, item in enumerate(related, 1):
            out += insight_markdown(profile, item, number)
    failures = [t for t in tests if t.get("longrepr")]
    if failures and single:
        out += ["", "## Failure", "", "```", failures[0]["longrepr"][-3000:], "```"]
    return "\n".join(out) + "\n"


def compare_report(old: dict[str, Any], new: dict[str, Any], *, top: int = 15) -> str:
    """What changed between two runs: speed, and (for correctness) outcomes and the set of tests."""
    old_tests = {t["nodeid"]: t for t in old["tests"]}
    new_tests = {t["nodeid"]: t for t in new["tests"]}
    out = [
        "# flakytest compare",
        "",
        f"Before: `{old['run']['id']}` · {headline(old)}",
        f"After:  `{new['run']['id']}` · {headline(new)}",
        "",
    ]
    common = old_tests.keys() & new_tests.keys()
    old_time = sum(old_tests[n]["duration"] for n in common)
    new_time = sum(new_tests[n]["duration"] for n in common)
    out.append(
        f"Tests in both runs: {len(common)}, their time {fmt(old_time)} → {fmt(new_time)} "
        f"({_delta(new_time - old_time, old_time)}). Wall time {fmt(old['run']['duration'])} → "
        f"{fmt(new['run']['duration'])}."
    )

    removed = sorted(old_tests.keys() - new_tests.keys())
    added = sorted(new_tests.keys() - old_tests.keys())
    changed = [
        (nodeid, old_tests[nodeid]["outcome"], new_tests[nodeid]["outcome"])
        for nodeid in sorted(common)
        if old_tests[nodeid]["outcome"] != new_tests[nodeid]["outcome"]
    ]
    out += ["", "## Correctness", ""]
    if not removed and not changed:
        out.append("✓ Every test from before ran again with the same outcome.")
    if changed:
        out.append(f"✗ {len(changed)} tests changed outcome:")
        out += [f"- `{nodeid}`: {before} → {after}" for nodeid, before, after in changed[:50]]
    if removed:
        out.append(f"✗ {len(removed)} tests from before didn't run (deleted, renamed, deselected or not collected):")
        out += [f"- `{nodeid}`" for nodeid in removed[:50]]
    if added:
        out.append(f"{len(added)} new tests.")

    deltas = sorted(
        ((new_tests[n]["duration"] - old_tests[n]["duration"], n) for n in common),
        key=lambda d: d[0],
    )
    faster = [d for d in deltas if d[0] < -0.02][:top]
    slower = [d for d in reversed(deltas) if d[0] > 0.02][:top]
    for title, rows in (("Faster", faster), ("Slower", slower)):
        if rows:
            out += ["", f"## {title}", "", "| Test | Before | After | Change |", "|---|---:|---:|---:|"]
            for delta, nodeid in rows:
                before = old_tests[nodeid]["duration"]
                out.append(
                    f"| `{nodeid}` | {fmt(before)} | {fmt(new_tests[nodeid]['duration'])} | {_delta(delta, before)} |"
                )

    out += ["", "## Categories", "", "| Category | Before | After |", "|---|---:|---:|"]
    categories = dict.fromkeys([*old["summary"]["categories"], *new["summary"]["categories"]])
    for category in categories:
        a = old["summary"]["categories"].get(category, 0.0)
        b = new["summary"]["categories"].get(category, 0.0)
        if max(a, b) >= 0.05:
            out.append(f"| {label(category)} | {fmt(a)} | {fmt(b)} |")
    return "\n".join(out) + "\n"


def _delta(delta: float, base: float) -> str:
    sign = "+" if delta >= 0 else "-"
    share = f", {sign}{abs(delta) / base:.0%}" if base else ""
    return f"{sign}{fmt(abs(delta))}{share}"
