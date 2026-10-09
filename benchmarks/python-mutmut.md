# Python backend experiment

The experiment does **not** justify replacing skillflow's native mutation
runner. The mutmut adapter works for the imported-code test subset, but the
full suite fails before mutation scoring. The subset also completes faster
with the native engine, which generates fewer mutations.

## Workload

Measured on 2026-10-09 using scratch clones of skillflow commit
`2cc54207514213b6aeb2c9f3f921505330358640`. The source selection was
`skills/skillflow/bin/skillflow.py`, lines 512 through 521, the
`last_reviewed` function. The interpreter was CPython 3.13.15 with pytest
9.1.1. Runs used four mutation workers inside skillflow's process sandbox.
The native baseline was mutator commit
`20637033e1becb6e91bae31d49b8ad729c9a0847`. The optional engine was mutmut
3.8.0 through this adapter.

Each invocation was cold, with `--mutate-all --no-coverage`. Timings include
setup, clean tests, mutation execution, and reports. These are single local
observations, not isolated-machine benchmarks. Other checks used the same
machine during parts of the experiment.

## Observations

- Native, full suite: **260.923 seconds**, 11 mutants, 9 killed and 2 survived.
  The unmutated baseline took 49.5 seconds.
- Native, full suite with `pytest -x`: **242.481 seconds**, but all 11 were
  reported killed. This is not an accepted performance result because the
  verdicts changed. Replaying the first disputed mutation alone with `-x`
  passed all 676 tests with 14 skips in 50.76 seconds. The cause of the
  parallel-run disagreement remains unresolved.
- Native, review suite with `pytest -x`: **9.132 seconds**, 11 mutants,
  9 killed and 2 survived.
- Mutmut, the same review suite and source lines: **20.001 seconds**,
  34 mutants, 31 killed and 3 survived. The mutant populations differ, so
  these timings do not measure engine speed on identical mutations.
- Mutmut, full suite: stopped with exit 2 during setup. Subprocess-based
  CLI tests could not execute the instrumented source under their separate
  interpreter and configuration. The failed run wrote no new Python snapshot.

The review subset used only `tests/test_review.py`, selected by changing
`tool.pytest.ini_options.testpaths` in the scratch copy. The same change was
used for both engines. The shared `n - 1` to `n + 1` mutant survived both.
Native's `1` to `0` mutant and mutmut's `1` to `2` mutant are distinct cases,
even when their reports name the same source line.

## Reproduce

Clone the fixed skillflow commit into disposable folders. Set
`MUTATOR_PYTHON` to the absolute Python path in an environment containing
this mutator checkout, pytest, and the `python` extra. Set `SANDBOX` to
skillflow's `skills/skillflow/bin/sandbox` launcher. From the scratch root:

```bash
"$SANDBOX" "$MUTATOR_PYTHON" -m mutator \
  --no-coverage --mutate-all --max-workers 4 --verbose \
  --test-command "$MUTATOR_PYTHON -m pytest -q -p no:cacheprovider" \
  --lines 512,513,514,515,516,517,518,519,520,521 \
  skills/skillflow/bin/skillflow.py
```

For the fail-fast comparison, add `-x` to the pytest command. For the
backend comparison, add `--python-backend mutmut`. Measure elapsed time
with the shell's `time`. Capture stdout, stderr, and the exit code. Keep
the interpreter, test suite, source, and worker cap fixed. Compare mutation
identities and outcomes as well as duration.

For the review subset, set `testpaths = ["tests/test_review.py"]` in each
scratch copy before either run. Do not treat that reduced suite as proof
for the whole CLI.

## Adoption decision

Keep the native backend as the default. The adapter is opt-in and starts
fresh on every invocation. Its tests verify report translation, line
selection, clean-run failures, worker cleanup, symlinks, Unicode offsets,
and invalidation after a test edit. Those checks establish the adapter's
bounded behavior; they do not establish compatibility with every Python
project or a speedup for skillflow.

Before changing skillflow's measurement command, capture the failing test
output for the two disputed native verdicts under parallel execution. A
future backend comparison should use a matched mutation corpus and require
the full project's clean tests to pass first.
