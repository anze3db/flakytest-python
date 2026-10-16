# flakytest's development tasks: `just` lists them. Everything runs through uv.

set shell := ["bash", "-euo", "pipefail", "-c"]
set positional-arguments

paths := "src tests scripts"

# List the recipes
[private]
default:
    @just --list --unsorted

# Install Python and the dev dependencies
sync:
    uv sync

# Run the tests; arguments go to pytest (`just test tests/test_hook.py -x`)
# tests/test_flake.py fails on purpose unless FLAKYTEST_SECRET_TOKEN is set and flakytest.dev mutes it
[group("test")]
test *args:
    uv run pytest "$@"

# Run the tests on another Python and pytest (`just test-on 3.11 6`)
[group("test")]
test-on python pytest="" *args:
    uv run --isolated --python "{{ python }}" {{ if pytest != "" { "--with 'pytest~=" + pytest + ".0'" } else { "" } }} pytest "${@:3}"

# Run the tests on every Python and pytest combination CI tests
[group("test")]
test-all:
    #!/usr/bin/env bash
    set -euo pipefail
    for py in 3.11 3.12 3.13 3.14 3.15; do
        for pytest in 6 7 8 9; do
            # pytest 6 uses ast.Str, which Python 3.14 removed
            [[ $pytest == 6 && ( $py == 3.14 || $py == 3.15 ) ]] && continue
            echo "── Python $py, pytest $pytest"
            just test-on "$py" "$pytest" -q
        done
    done

# Lint, format check and type check, as CI does
[group("check")]
lint:
    uv run ruff check {{ paths }}
    uv run ruff format --check {{ paths }}
    uv run mypy

# Format the code and apply ruff's safe fixes
[group("check")]
fmt:
    uv run ruff format {{ paths }}
    uv run ruff check --fix {{ paths }}

# Audit the GitHub workflows (zizmor, actionlint)
[group("check")]
lint-actions:
    uvx zizmor .github/workflows
    uvx --from actionlint-py actionlint

# Everything before a push: lint, then the tests
[group("check")]
check: lint test

# Preview the next release; `just release --no-dry-run` commits and pushes it, then waits for it
[group("release")]
release *args:
    #!/usr/bin/env bash
    set -euo pipefail
    commit=false
    rest=()
    for arg in "$@"; do
        if [[ $arg == --no-dry-run ]]; then commit=true; else rest+=("$arg"); fi
    done
    if $commit; then
        uv run scripts/release.py prepare ${rest[@]+"${rest[@]}"}
    else
        uv run scripts/release.py prepare --dry-run ${rest[@]+"${rest[@]}"}
        echo
        echo "That was a dry run. To release it: just release --no-dry-run"
        echo "(commits it, pushes it to main and waits until CI passes and it's published)"
    fi

# Remove build, coverage and cache output
clean:
    rm -rf dist htmlcov .coverage .coverage.* .pytest_cache .ruff_cache .mypy_cache
