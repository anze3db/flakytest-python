#!/bin/bash
# Benchmark wrapper: BENCH_PYSPY_BLOCKING=1 drops --nonblocking, BENCH_PYSPY_EXTRA adds arguments to `record`
args=()
for a in "$@"; do
  if [[ "$a" == "--nonblocking" && "${BENCH_PYSPY_BLOCKING:-0}" == "1" ]]; then continue; fi
  args+=("$a")
done
if [[ "${args[0]}" == "record" && -n "${BENCH_PYSPY_EXTRA:-}" ]]; then
  args=("record" $BENCH_PYSPY_EXTRA "${args[@]:1}")
fi
exec "$(dirname "$0")/py-spy-real" "${args[@]}"
