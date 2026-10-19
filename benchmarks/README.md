# Profiler benchmarks

How much py-spy (Python 3.14) and tachyon (Python 3.15's `profiling.sampling`) cost, how many samples they lose,
and whether the lost samples bias the profile. The write-up is at
https://flakytest.dev/guides/py-spy-vs-tachyon/.

## Setup

```console
$ ./setup.sh
```

This creates `env3.14` (with py-spy) and `env3.15` (tachyon), both with the plugin from this checkout, and installs
two switches the benchmarks use:

- `BENCH_PYSPY_BLOCKING=1`: `env3.14/bin/py-spy` is a wrapper (`py-spy-wrapper.sh`) that drops `--nonblocking` from
  the command the plugin runs.
- `BENCH_TACHYON_BLOCKING=1`: a `.pth` hook (`bench_tachyon_blocking.py`) that makes the plugin's tachyon sampler
  pass `blocking=True` to `SampleProfiler`. It only acts in the sampler process, never in the measured one.

## Benchmarks

Each appends one JSON line per run, in shuffled order, and can be stopped and resumed.

```console
$ python3 micro/micro.py micro/results.jsonl 3   # one busy process at stack depth 10/50/150, every sampler setting
$ python3 micro/bias.py 3                         # flat loop vs constantly changing stack, 50/50: what does each report?
$ python3 suites.py suites.jsonl 5                # the cpu, deep and django suites through pytest --flakytest-profile
$ python3 analyze.py                              # Markdown tables (medians) of all three
```

- `micro/target.py` is the microbenchmark's target: it prints its throughput, so the slowdown under each sampler is
  measured against an unprofiled run on the same Python.
- `suites/` holds the three suites. Each one's `conftest.py` records wall-clock marks and CPU time for the harness;
  `cpu` and `deep` also record how long every test really spent in `work_a` and `work_b`, so `suites.py` can compare
  the profile's split with the truth (`share_error_pp`).
- `attach_stats.py PID blocking|nonblocking` attaches tachyon to a running process for 4 seconds and prints its
  unwinder statistics (frame cache, code object cache, reads). `depth.py PID` prints the sampled stack depths.

`suites.py` gives every run 4 minutes: py-spy in blocking mode at 100 Hz and up can slow a suite down by orders of
magnitude, and it pauses the target with ptrace, so the harness kills the samplers as well as pytest.

## Results

`micro/results.jsonl`, `micro/bias.jsonl` and `suites.jsonl` are the runs behind the write-up: a 4 vCPU Intel Xeon
cloud VM with 16 GB of RAM, CPython 3.14.6 with py-spy 0.4.2, and CPython 3.15.0b4. The two `py-spy-blocking` rows at
500 Hz and 100 Hz on the django suite in the write-up were single runs outside `suites.py`'s matrix (the first one
is the one 500 Hz row in `suites.jsonl`).
