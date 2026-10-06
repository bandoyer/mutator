# Worker cleanup

Each file's mutants run in worker folders under `target/mutation-workers/run-<id>/worker-<n>`. When the file's mutants finish, mutator removes the whole `run-<id>` folder. The run must not crash while removing it, even when the test command started a process that is still writing in the worker.

When mutator gets SIGTERM (what `timeout` and `kill` send) or Ctrl-C, it stops every test command it is running and removes the folder before it exits. To stop a command, it sends SIGTERM to the command's process group, waits up to 1 s, then sends SIGKILL. mutator reaps a command's leader only after it has sent SIGKILL to the rest of its group, so it never signals a group id that may already belong to someone else (issue #44). A mutator run nested in a test command therefore gets the same chance to clean up. SIGKILL to mutator itself can't be caught, so after it the folder and the test commands stay.

## Sub-features

- `cleanup-normal` leaves no folder in `target/mutation-workers` after an ordinary run.
- `cleanup-leftover-process` finishes without `OSError: Directory not empty` when the test command leaves a process writing in the worker's `target/debug/deps`, and leaves no worker folder.
- `cleanup-sigterm` stops the test command and removes the folder when mutator gets SIGTERM during a worker's run, and exits `143`.
- `cleanup-ctrl-c` does the same for Ctrl-C (SIGINT), during the baseline and during a worker's run.
- `cleanup-order` sends no signal to a test command's group after mutator has reaped that group's leader, for a normal run, a test command that leaves a background process, and a baseline timeout.
- `cleanup-ctrl-c-held` stops a background process that still holds the test command's output when Ctrl-C comes after the command's shell has exited: during the baseline, and during a timeout's 1 s grace.
- `cleanup-nested-timeout` lets a mutator run nested in a test command clean up when the outer run's timeout stops that command (issue #21).
- `nested-run-refused` refuses a mutator run whose project root is inside `target/mutation-workers`, before it runs any command (issue #57), also when `target` is a symlink to storage elsewhere. A mutant in mutator's own tests can start such a run in the worker it is tested in, and each one would start the next.

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

- **cleanup-sigterm.** `project=$($vm project fixture)`. Run `$vm signal "$project" "$T" TERM 3 --no-coverage --mutate-all --max-workers 1 --test-command 'case "$PWD" in *mutation-workers*) sleep 30;; esac; true' demo.py`. The baseline passes at once, and the control run in worker-0 is still in `sleep 30` when SIGTERM arrives. Pass: exit code `143`, no traceback, worker folders `(none)`, and processes left `(none)`. The bug shows as a `run-<id>` folder and `sleep 30` left behind. Exit code `137` means mutator was still running 2 s after the signal.
- **cleanup-ctrl-c.** Two drives, each with a fresh fixture project:
  - During a worker's run: the same command as `cleanup-sigterm`, with `INT` in place of `TERM`.
  - During the baseline: `$vm signal "$project" "$T" INT 3 --no-coverage --mutate-all --max-workers 1 --test-command 'sleep 30' demo.py`.

  Pass for both: exit code `130`, stderr ends with Python's `KeyboardInterrupt`, worker folders `(none)`, and processes left `(none)`. The bug shows as `sleep 30` left behind, and, during a worker's run, as exit code `137`: mutator hung waiting for the worker until the SIGKILL.
- **cleanup-order.** Three drives, each with a fresh fixture project, through `$vm trace`, which runs `./mutator` under `strace -f` and lists each group signal sent after the wait that reaped that group's leader:
  - `$vm trace "$project" "$T" --no-coverage --mutate-all --max-workers 1 demo.py`. Exit code `0`.
  - `$vm trace "$project" "$T" --no-coverage --mutate-all --max-workers 1 --test-command '(sleep 0.3; touch late) >/dev/null 2>&1 &' demo.py`. Exit code `3`. Then no `late` file in the project or a worker.
  - `$vm trace "$project" "$T" --no-coverage --mutate-all --max-workers 1 --baseline-timeout 1 --test-command 'sleep 30' demo.py`. Exit code `2`.

  Pass for each: the section `group signals sent after their leader was reaped` says `(none)` under a nonzero count of group signals, worker folders `(none)`, and processes left `(none)`. The bug shows as `kill(-<pid>, SIGKILL)` lines in that section, one or more per test command.
- **cleanup-ctrl-c-held.** Two drives, each with a fresh fixture project. The test command's shell exits at once, but its background `sleep 30` keeps the output pipe open:
  - During the baseline: `$vm signal "$project" "$T" INT 2 --no-coverage --mutate-all --max-workers 1 --test-command 'case "$PWD" in *mutation-workers*) exit 0;; esac; sleep 30 & exit 0' demo.py`.
  - During the timeout's grace: the shell exits on SIGTERM, and its background sleep ignores SIGTERM. `$vm signal "$project" "$T" INT 1.6 --no-coverage --mutate-all --max-workers 1 --baseline-timeout 1 --test-command "case \"\$PWD\" in *mutation-workers*) exit 0;; esac; trap 'exit 0' TERM; (trap '' TERM; exec sleep 30) & wait" demo.py`.

  Pass for both: exit code `130`, worker folders `(none)`, and processes left `(none)`. The bug shows as `sleep 30` in processes left.
- **cleanup-nested-timeout.** The outer test command, in its worker, runs a second mutator in `inner/`. The worker reaches `inner/` through a link, so the inner run's root is the project's own `inner/`, outside `target/mutation-workers`, and `nested-run-refused` doesn't apply. The inner control run keeps writing files in the inner worker, so the outer control run hangs until `--baseline-timeout 3` stops it:

  ```bash
  project=$($vm project fixture)
  scripts=$(dirname "$project")
  mkdir "$project/inner" && cp "$project/demo.py" "$project/inner/"
  printf 'case "$PWD" in */inner/target/mutation-workers/*) end=$(( $(date +%%s) + 30 )); i=0; while [ "$(date +%%s)" -lt "$end" ]; do : > "w$i"; i=$((i+1)); done;; esac\nexit 0\n' >"$scripts/inner.sh"
  printf 'case "$PWD" in */target/mutation-workers/*) cd inner && exec %s --no-coverage --mutate-all --max-workers 1 --test-command "sh %s/inner.sh" demo.py;; esac\nexit 0\n' "$PWD/mutator" "$scripts" >"$scripts/outer.sh"
  $vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --baseline-timeout 3 --test-command "sh $scripts/outer.sh" demo.py
  ls -A "$project/inner/target/mutation-workers"
  ```

  Run it from the repo root, so `$PWD/mutator` is this checkout's launcher. Pass: exit code `2`, stderr says `Unmutated tests timed out after 3 s in a mutation worker for demo.py`, no traceback, worker folders `(none)`, processes left `(none)`, and the last `ls` prints nothing: the inner run removed its own `run-<id>` folder. The bug shows as a `run-<id>` folder in `inner/target/mutation-workers`, and `sh .../inner.sh` left running.
- **nested-run-refused.** The outer test command, in its worker, runs a second mutator there with no `--root`, so its root is the worker. That inner run's test command only touches a file, so the recipe stays small even where the refusal is missing:

  ```bash
  project=$($vm project fixture)
  scripts=$(dirname "$project")
  printf 'case "$PWD" in */run-*/worker-*) exec %s --no-coverage --mutate-all --max-workers 1 --test-command "touch %s/nested-ran" demo.py;; esac\nexit 0\n' "$PWD/mutator" "$scripts" >"$scripts/nest.sh"
  $vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --test-command "sh $scripts/nest.sh" demo.py
  ls "$scripts/nested-ran"
  ```

  Run it from the repo root. Pass: exit code `2` within seconds, stderr says `Unmutated tests failed in a mutation worker for demo.py` and `mutator does not run in <project>/target/mutation-workers/run-<id>/worker-0: it is inside target/mutation-workers`, worker folders `(none)`, processes left `(none)`, and `ls` finds no `nested-ran`: the refused run started no command. The bug shows as a `nested-ran` file and no `does not run in` line: the inner run mutated the outer worker. In mutator's own tests, that inner run's tests start the next one (#57).

  Then drive it once more with a fresh fixture whose `target` is a symlink to storage outside the project, made before the drive: `mkdir "$scripts/storage" && ln -s "$scripts/storage" "$project/target"`. The same pass holds, with the worker under `$scripts/storage/mutation-workers` in the refusal. A worker's real path then has no `target/mutation-workers` in it, and the run folder's `.mutator-run` file is what mutator recognizes.

## Gotchas

- `trace` needs `strace`; without it the subcommand stops and names the package to install. Where ptrace is blocked (some agent sandboxes), strace records nothing; `trace` then says so and exits 1, so a blocked trace never reads as `(none)`. Run it inside `bin/sandbox`, like every drive.

- Run each signal or nested recipe inside one `bin/sandbox` call. A test command left behind stops by itself after 30 s, and the sandbox ends it sooner. The transcript's `processes left` section is written before the sandbox ends, so it is the check.
- `signal` gives mutator's own exit code. A mutator that a signal killed outright (the bug) shows as `128 +` the signal, the same `143` as a clean SIGTERM exit, so judge `cleanup-sigterm` by the folders and processes left, not the code alone.

- The leftover writer also runs during the baseline, in the scratch project's own `target/debug/deps`. That is expected and is removed by cleanup.
- Without `--mutate-all`, a project that already has `.metrics/mutate` reruns nothing, so the recipe proves nothing. Keep the flag.
