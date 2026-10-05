# Worker cleanup

Each file's mutants run in worker folders under `target/mutation-workers/run-<id>/worker-<n>`. When the file's mutants finish, mutator removes the whole `run-<id>` folder. The run must not crash while removing it, even when the test command started a process that is still writing in the worker.

## Sub-features

- `cleanup-normal` leaves no folder in `target/mutation-workers` after an ordinary run.
- `cleanup-leftover-process` finishes without `OSError: Directory not empty` when the test command leaves a process writing in the worker's `target/debug/deps`, and leaves no worker folder.

## How to get to it (user POV)

- Run any mutator command that runs mutants, then look in `target/mutation-workers`.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `project=$($vm project fixture)` gives the Python fixture. `T` is the transcript path for the feature.

- **cleanup-normal.** Run `$vm drive "$project" "$T" --no-coverage --mutate-all demo.py`. Exit code `0`. Worker folders `(none)`.
- **cleanup-leftover-process.** Set the command that leaves a writer behind, as a dying `rustc` can:

  ```bash
  leftover='mkdir -p target/debug/deps; (cd target/debug/deps && seq 3000 | xargs touch); (i=0; while [ $i -lt 3000 ]; do : > target/debug/deps/late$i; i=$((i+1)); done) >/dev/null 2>&1 &'
  ```

  Run `$vm drive "$project" "$T" --no-coverage --mutate-all --test-command "$leftover" demo.py`. Pass: exit code `3` (the command always passes, so every mutant survives), no `Directory not empty` or `Traceback` in stderr, and worker folders `(none)`. The bug shows as exit code `1`, `OSError: [Errno 39] Directory not empty: '.../worker-<n>/target/debug/deps'`, and a `run-<id>` folder left behind.

## Gotchas

- The leftover writer also runs during the baseline, in the scratch project's own `target/debug/deps`. That is expected and is removed by cleanup.
- Without `--mutate-all`, a project that already has `.metrics/mutate` reruns nothing, so the recipe proves nothing. Keep the flag.
