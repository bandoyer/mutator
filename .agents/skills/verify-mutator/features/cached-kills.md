# Cached kills

Once `.metrics/mutate` exists, a run without `--mutate-all` keeps a killed mutant from the snapshot instead of running it again. It keeps the kill only when nothing the kill depends on has changed: every file git lists for the project (apart from mutator's own `.metrics/` and `target/` output), the test command, the folder it runs in, and `--timeout-factor`. Any change reruns every kept kill. Without a git repository, nothing is kept. When kills are kept, the baseline still runs, so tests that fail now stop the run with exit `2`.

## Sub-features

- `cached-kill-unchanged` keeps the kills when nothing changed, and runs only the baseline.
- `cached-kill-test-weakened` reruns a kept kill after the test's assertion is deleted, and reports it `SURVIVED` with exit 3.
- `cached-kill-command-fails` stops with exit 2 when every site is cached and the test command now fails, and leaves the snapshot as it was.
- `cached-kill-bujo` on a real project: a test command that runs no test reports bujo's kept kills as survivors.

## How to get to it (user POV)

- Run `mutator <file>` once, change the project or the test command, and run `mutator <file>` again from the project root.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `project=$($vm project fixture)` gives the Python fixture, a git repository. `T` is the transcript path for the feature. Use a fresh project for each sub-feature.

- **cached-kill-unchanged.** Run `$vm drive "$project" "$T" --no-coverage demo.py`, then `$vm drive "$project" "$T" --no-coverage --verbose demo.py`. The second run exits `0`, prints the three `KILLED    demo.py:2` lines and `100.0%`, and its stderr shows one test command run (the baseline) and no `Mutant timeout` line, because no mutant runs.
- **cached-kill-test-weakened.** Run `$vm drive "$project" "$T" --no-coverage demo.py` (exit `0`, three `KILLED`). Then `$vm exec "$project" "$T" sed -i 's/assert f() is True/f()/' test_demo.py`, and `$vm drive "$project" "$T" --no-coverage demo.py`. The second drive exits `3`, and every mutant line reads `SURVIVED`.
- **cached-kill-command-fails.** Run `$vm drive "$project" "$T" --no-coverage demo.py` (exit `0`). Then `$vm exec "$project" "$T" stat -c %y .metrics/mutate/demo.edn`, `$vm drive "$project" "$T" --no-coverage --test-command false demo.py`, and the same `stat` again. The drive exits `2`, stderr starts `Baseline failed for demo.py: false` and has no `Wrote` line, stdout has no `KILLED` line, and both modification times are equal.
- **cached-kill-bujo.** `project=$($vm project ~/Work/bujo)`. Run `$vm drive "$project" "$T" --no-coverage src` (exit `0`, `Total ... 12 0 0 12 100.0%`). Then `$vm drive "$project" "$T" --no-coverage --test-command "cargo test --no-run" src`. It compiles and runs no test, so it exits `3` with `Total ... 0 12 0 12 0.0%`. For the unchanged case, in a fresh clone, run `$vm drive "$project" "$T" --no-coverage src` twice, the second time with `--verbose`: it exits `0` with 12/12 killed, and stderr shows one test command, the baseline.

## Gotchas

- Compare the snapshot's modification time, not its bytes. A rewrite in the same second as the first run writes the same bytes, because `tested-at` has one-second resolution.
- A file that changes on every run and that git doesn't ignore, such as a coverage report written into the project, changes the context each time, so nothing is kept. The fixture ignores `target/` and `.metrics/`, and these recipes pass `--no-coverage`.
- bujo keeps `.metrics/` untracked and not ignored. mutator leaves its own `.metrics/` out of the context, so the bujo unchanged case still keeps its kills.
