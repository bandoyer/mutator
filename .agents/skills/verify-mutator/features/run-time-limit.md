# Run time limit

Mutator runs the unmutated tests before any mutant: the baseline run in the project, then one control run in each worker. Neither has the mutant timeout, which is taken from the baseline. `--baseline-timeout <seconds>` limits both runs (default 600). When either run reaches the limit, mutator stops the test command's process group (SIGTERM, then SIGKILL after 1 s), stops the file with exit code 2, and says which run timed out and what the limit was.

## Sub-features

- `limit-baseline-hang` stops a test command that hangs in the project.
- `limit-control-hang` stops a test command that passes in the project but hangs in a worker.
- `limit-term-ignored` stops a test command that ignores SIGTERM.
- `limit-option` lists the option and its default in `--help`, and rejects `0`.

## How to get to it (user POV)

- Run any mutator command that runs mutants, with or without `--baseline-timeout`.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `T` is the transcript path for the feature.
- Run each hang recipe inside one `bin/sandbox` call. A process left behind dies with its sandbox, so the transcript's `processes left` section is the check, and it is written before the sandbox ends.

- **limit-baseline-hang.** `project=$($vm project fixture)`. Run `$vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --baseline-timeout 3 --test-command 'sleep 1000' demo.py`. Pass: exit code `2` after about 3 s, stderr says the baseline timed out after 3 s, no `KILLED` line, worker folders `(none)`, and processes left `(none)`. The bug shows as a drive that never returns.
- **limit-control-hang.** The same, with `--test-command 'case "$PWD" in *mutation-workers*) sleep 1000;; esac; true'`. The baseline passes in the project, and the control run in worker-0 hangs. Pass: exit code `2` after about 3 s, stderr says the unmutated tests timed out in a mutation worker after 3 s, no `KILLED` line, no `.metrics/mutate/demo.edn` written, worker folders `(none)`, and processes left `(none)`. The bug shows as a drive that never returns.
- **limit-term-ignored.** The same as `limit-baseline-hang`, with `--test-command "trap '' TERM; sleep 1000"`. Pass: exit code `2` after about 4 s (the limit plus the 1 s grace), stderr says the baseline timed out after 3 s, worker folders `(none)`, and processes left `(none)`.
- **limit-option.** `project=$($vm project fixture)`. Run `$vm drive "$project" "$T" --help` and `$vm drive "$project" "$T" --baseline-timeout 0 demo.py`. Pass: the help lists `--baseline-timeout <seconds>` with `Default: 600`, and the second command exits `1` with a message that the option needs a positive number.

## Gotchas

- Wrap a drive that may hang in `timeout`, such as `timeout -k 2 30 $vm drive ...`, so a regression ends the recipe instead of the session. Exit `124` then means the bug is back. A drive that `timeout` kills writes no transcript, so save the console output as the proof.
