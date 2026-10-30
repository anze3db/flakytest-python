"""Where a test session runs: CI provider or developer machine, git state, platform."""

import contextlib
import getpass
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

# A container's memory limit: cgroup v2, then v1 (which says "no limit" with a huge number)
CGROUP_MEMORY_LIMITS = ("/sys/fs/cgroup/memory.max", "/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_CPU_LIMIT = "/sys/fs/cgroup/cpu.max"

GIT_LOG_FORMAT = {
    "commit_hash": "%H",
    "tree_hash": "%T",
    "parent_hash": "%P",
    "author_name": "%an",
    "author_email": "%ae",
    "author_date": "%aI",
    "committer_name": "%cn",
    "committer_email": "%ce",
    "committer_date": "%cI",
    "ref_names": "%D",
    "encoding": "%e",
    "subject": "%s",
    "body": "%b",
    "notes": "%N",
    "signature_status": "%G?",
    "signer_name": "%GS",
    "signer_key": "%GK",
    "signer_fingerprint": "%GF",
    "signer_trustlevel": "%GF",
}
MAX_CHANGED_FILES = 500


def run_git_command(command: list[str]) -> str | None:
    try:
        process = subprocess.Popen(command, shell=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        return process.communicate()[0].strip().decode()
    except:
        return None


def get_git_data() -> dict[str, Any]:
    delimiter = "%n-___-___-%n"
    log = run_git_command(["git", "log", "-1", f"--pretty=format:{delimiter.join(GIT_LOG_FORMAT.values())}"])
    if log:
        log_dict: dict[str, Any] = dict(zip(GIT_LOG_FORMAT, log.split(delimiter.replace("%n", "\n")), strict=False))
    else:
        return {}

    branch = run_git_command(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    if branch:
        log_dict["branch"] = branch

    # What the tests ran against: HEAD's own changes plus anything uncommitted. Lets flakytest.dev tell a
    # fix to the code (the test caught a regression) from a fix to the test (churn).
    status = run_git_command(["git", "status", "--porcelain", "--untracked-files=normal"]) or ""
    dirty = [line[3:].split(" -> ")[-1].strip('"') for line in status.splitlines() if len(line) > 3]
    in_head = (run_git_command(["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"]) or "").splitlines()
    log_dict["dirty"] = bool(dirty)
    log_dict["changed_files"] = sorted(set(dirty) | set(in_head))[:MAX_CHANGED_FILES]
    return log_dict


def detect_environment(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """{"kind", "name", "provider", "details"}; name is the stable key runs are grouped by."""
    env = os.environ if environ is None else environ
    override = env.get("FLAKYTEST_ENV")
    if env.get("GITHUB_ACTIONS") == "true":
        details = {
            "workflow": env.get("GITHUB_WORKFLOW"),
            "job": env.get("GITHUB_JOB"),
            "runner_os": env.get("RUNNER_OS"),
            "runner_arch": env.get("RUNNER_ARCH"),
            "runner_name": env.get("RUNNER_NAME"),
        }
        result = _ci("github-actions", env.get("GITHUB_JOB"), details)
    elif env.get("GITLAB_CI"):
        details = {
            "job": env.get("CI_JOB_NAME"),
            "stage": env.get("CI_JOB_STAGE"),
            "runner": env.get("CI_RUNNER_DESCRIPTION"),
        }
        result = _ci("gitlab", env.get("CI_JOB_NAME"), details)
    elif env.get("CIRCLECI"):
        result = _ci("circleci", env.get("CIRCLE_JOB"), {"job": env.get("CIRCLE_JOB")})
    elif env.get("BUILDKITE"):
        label = env.get("BUILDKITE_LABEL")
        result = _ci("buildkite", label, {"label": label, "agent": env.get("BUILDKITE_AGENT_NAME")})
    elif env.get("JENKINS_URL"):
        result = _ci("jenkins", env.get("JOB_NAME"), {"job": env.get("JOB_NAME"), "node": env.get("NODE_NAME")})
    elif env.get("CI", "").lower() in ("true", "1", "yes"):
        result = _ci("ci", None, {})
    else:
        try:
            user = getpass.getuser()
        except Exception:
            user = "unknown"
        host = platform.node().split(".")[0] or "unknown"
        result = {"kind": "local", "name": f"local:{user}@{host}", "provider": "local", "details": {}}
    if override:
        result["name"] = override
    result["details"] = {k: v for k, v in result["details"].items() if v}
    return result


def _ci(provider: str, job: str | None, details: dict[str, Any]) -> dict[str, Any]:
    name = f"ci:{provider}:{job}" if job else f"ci:{provider}"
    return {"kind": "ci", "name": name, "provider": provider, "details": details}


def platform_info() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "usable_cpus": usable_cpus(),
        "memory": machine_memory(),
    }


def usable_cpus() -> float | None:
    """The CPUs tests can keep busy at full speed, when that's fewer than cpu_count: the performance cores of Apple
    silicon, the CPUs this process may run on, or the container's CPU limit."""
    counts: list[float] = []
    if sys.platform == "darwin":
        with contextlib.suppress(OSError, ValueError, subprocess.SubprocessError):
            output = subprocess.run(
                ["sysctl", "-n", "hw.perflevel0.logicalcpu"], capture_output=True, text=True, timeout=2, check=True
            ).stdout
            counts.append(int(output))
    with contextlib.suppress(AttributeError, OSError):
        counts.append(len(os.sched_getaffinity(0)))
    with contextlib.suppress(OSError, ValueError):  # cgroup v2: "max 100000" without a limit
        quota, period = Path(CGROUP_CPU_LIMIT).read_text().split()
        if quota != "max":
            counts.append(int(quota) / int(period))
    usable = min(counts) if counts else None
    return round(usable, 2) if usable is not None and usable < (os.cpu_count() or 1) else None


def machine_memory() -> int | None:
    """The memory the tests can use, in bytes: the container's limit if it has one, else the machine's."""
    limits = []
    for path in CGROUP_MEMORY_LIMITS:
        with contextlib.suppress(OSError, ValueError):  # "max": no limit
            limits.append(int(Path(path).read_text().strip()))
    with contextlib.suppress(OSError, ValueError, AttributeError):  # no sysconf on Windows
        limits.append(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES"))
    return min(limits) if limits else None


def python_info() -> dict[str, Any]:
    return {"version": platform.python_version(), "implementation": sys.implementation.name}
