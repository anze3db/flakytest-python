#!/bin/bash
# Create the two environments the benchmarks run in: Python 3.14 with py-spy, Python 3.15 with tachyon.
# Both install the plugin from this checkout.
set -euo pipefail
cd "$(dirname "$0")"
for v in 3.14 3.15; do
  uv venv -q --python "$v" "env$v"
  VIRTUAL_ENV="$PWD/env$v" uv pip install -q -e "..[py-spy]" pytest-xdist pytest-django django
done
# py-spy wrapper: BENCH_PYSPY_BLOCKING=1 drops --nonblocking from the command the plugin runs
mv env3.14/bin/py-spy env3.14/bin/py-spy-real
cp py-spy-wrapper.sh env3.14/bin/py-spy
chmod +x env3.14/bin/py-spy
# tachyon hook: BENCH_TACHYON_BLOCKING=1 makes the plugin's tachyon sampler pause the target for each sample
site=$(env3.15/bin/python -c "import site; print(site.getsitepackages()[0])")
cp bench_tachyon_blocking.py "$site/"
echo "import bench_tachyon_blocking; bench_tachyon_blocking.install()" > "$site/bench_tachyon_blocking.pth"
