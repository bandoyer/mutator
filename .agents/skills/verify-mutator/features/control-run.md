# Control runs

Before a worker runs its first mutant, it runs the project's tests once on the unmutated code, with no timeout. That run builds the worker's own `target/`, which workers don't share with the project. Mutant runs then start warm, so the timeout taken from the project's baseline is fair. When the control run fails, the worker can't run the tests at all: mutator stops the file with exit code 2 and records no kills.

## Sub-features

- `control-cold-build` lets a mutant survive when the worker's first build is slower than the timeout and the tests can't catch the mutant.
- `control-bujo-weak` runs bujo with a test command that can't fail: every mutant survives.
- `control-broken-worker` stops with exit code 2 when the test command works in the project but not in a worker.

## How to get to it (user POV)

- Run any mutator command that runs mutants.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `T` is the transcript path for the feature.
- Run every step through `bin/sandbox` when skillflow asks for it.

- **control-cold-build.** `project=$($vm project cold-build)`. Warm the project's own build with `$vm exec "$project" "$T" cargo test -q` (exit `0`, about 7 s). Then run `$vm drive "$project" "$T" --no-coverage --mutate-all src/lib.rs`. Pass: exit code `3`, `SURVIVED  src/lib.rs:2 1 -> 0`, `Total ... 0 1 0 1 0.0%`, and worker folders `(none)`. The bug shows as `KILLED`, exit `0`, after about 2 s.
- **control-bujo-weak.** `project=$($vm project ~/Work/bujo)`. Warm it with `$vm exec "$project" "$T" cargo test -q`. Then run `$vm drive "$project" "$T" --mutate-all --no-coverage --test-command 'cargo test >/dev/null 2>&1; true' src`. Pass: exit code `3`, every mutant line reads `SURVIVED` (12 at bujo 878b1a8), and worker folders `(none)`. The bug shows as every mutant `KILLED` and exit `0`.
- **control-broken-worker.** `project=$($vm project fixture)`. Give it a virtualenv with `$vm exec "$project" "$T" python3 -m venv .venv`. Then run `$vm drive "$project" "$T" --no-coverage --max-workers 1 --test-command './.venv/bin/python -c "import demo; assert demo.f() in (True, False)"' demo.py`. The assertion accepts the original and every mutant, and works in the project. Workers don't link `.venv`, so it fails there. Pass: exit code `2`, stderr says the unmutated tests failed in a worker, no `KILLED` line, no `.metrics/mutate/demo.edn` written, and worker folders `(none)`. The bug shows as three `KILLED` lines and exit `0`.

## Gotchas

- Warm the project before driving. A cold project makes the baseline slow, and the timeout grows to match, which hides the bug.
- The cold-build fixture's build script runs again whenever `build.rs` changes, not when `src/` changes, so a mutant run in a warm worker takes well under a second.
