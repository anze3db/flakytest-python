"""Plain text and Markdown views of a profile: the terminal summary, and reports written for people and
coding agents (`flakytest report`)."""

from collections import Counter
from pathlib import Path
from typing import Any

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
        parts.append(f"{segment['label']} ({segment['phase']}) {fmt(segment['seconds'])}")
    if test.get("shared_fixture_time", 0) >= 0.05:
        parts.append(f"incl. {fmt(test['shared_fixture_time'])} shared fixture setup")
    db = test.get("db") or {}
    if db.get("queries"):
        parts.append(f"{db['queries']} queries")
    return ", ".join(parts)


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


def terminal_summary(profile: dict[str, Any], report_path: Path, upload_url: str | None = None) -> list[str]:
    summary = profile["summary"]
    total = summary["test_time"]
    lines = [headline(profile)]
    categories = [(k, v) for k, v in summary["categories"].items() if v / (total or 1) >= 0.02][:6]
    if categories:
        lines.append("Time goes to: " + " · ".join(f"{label(k)} {pct(v, total)}" for k, v in categories))
    slowest = sorted(profile["tests"], key=lambda t: -t["duration"])[:5]
    if slowest:
        lines.append("Slowest tests:")
        lines.extend(f"  {fmt(t['duration']):>7}  {t['nodeid']}  ({why(t)})" for t in slowest)
    if profile["insights"]:
        lines.append("Fix next:")
        for number, item in enumerate(profile["insights"][:3], 1):
            lines.append(f"  {number}. [{item['severity']}] {item['title']} (~{fmt(item['impact'])} per run)")
    lines.extend(f"⚠ {warning['message']}" for warning in profile["warnings"] if warning["code"] != "short_tests")
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
    for test in sorted(profile["tests"], key=lambda t: -t["duration"])[:top]:
        p = test["phases"]
        out.append(
            f"| `{test['nodeid']}` | {fmt(test['duration'])} | {fmt(p['setup'])} / {fmt(p['call'])} / "
            f"{fmt(p['teardown'])} | {why(test)} |"
        )

    files: Counter[str] = Counter()
    for test in profile["tests"]:
        files[test["file"]] += test["duration"]
    out += ["", "## Slowest files", "", "| File | Time | Tests |", "|---|---:|---:|"]
    counts = Counter(test["file"] for test in profile["tests"])
    for file, seconds in files.most_common(min(top, 10)):
        out.append(f"| `{file}` | {fmt(seconds)} | {counts[file]} |")

    fixtures = profile.get("fixtures", [])[:10]
    if fixtures:
        out += ["", "## Slowest fixtures (exact setup time)", "", "| Fixture | Scope | Calls | Total | Mean |"]
        out.append("|---|---|---:|---:|---:|")
        out += [
            f"| `{f['name']}` | {f['scope']} | {f['calls']} | {fmt(f['total'])} | {fmt(f['mean'])} |" for f in fixtures
        ]

    if profile["warnings"]:
        out += ["", "## Warnings", ""]
        out += [f"- {warning['message']}" for warning in profile["warnings"]]

    out += [
        "",
        "## Next steps",
        "",
        "- `flakytest report --test '<nodeid>'` shows one test's call tree, hot lines, fixtures and queries.",
        (
            "- After a change, rerun the affected tests with `flakytest run -- <tests>`, then "
            "`flakytest compare` to check the speed-up and that every test still has the same outcome."
        ),
    ]
    return "\n".join(out) + "\n"


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
                f"Includes {fmt(test['shared_fixture_time'])} of class/module/session fixture setup that this test "
                "triggered first (other tests reuse it)."
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
