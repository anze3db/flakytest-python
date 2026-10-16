from flakytest.environment import detect_environment


def test_github_actions():
    env = {"GITHUB_ACTIONS": "true", "GITHUB_JOB": "test", "GITHUB_WORKFLOW": "CI", "RUNNER_OS": "Linux"}
    assert detect_environment(env) == {
        "kind": "ci",
        "name": "ci:github-actions:test",
        "provider": "github-actions",
        "details": {"workflow": "CI", "job": "test", "runner_os": "Linux"},
    }


def test_gitlab_and_generic_ci():
    assert detect_environment({"GITLAB_CI": "true", "CI_JOB_NAME": "pytest"})["name"] == "ci:gitlab:pytest"
    assert detect_environment({"CI": "true"})["name"] == "ci:ci"


def test_local_machine():
    environment = detect_environment({})
    assert environment["kind"] == "local"
    assert environment["name"].startswith("local:")


def test_override():
    assert detect_environment({"CI": "1", "FLAKYTEST_ENV": "ci:nightly"})["name"] == "ci:nightly"
