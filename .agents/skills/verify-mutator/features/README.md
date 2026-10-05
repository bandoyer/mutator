# mutator verification map

This folder is the maintained source for verifying what a mutator user sees. Read this index before driving the CLI, then use the matching feature file as the recipe.

## Baseline preconditions

- `$vm doctor` prints `doctor: ok` for the commit under test (`vm=.agents/skills/verify-mutator/bin/verify-mutator`).
- Each recipe starts from a fresh scratch project from `$vm project`.
- No real project folder is driven. A real project is cloned first.

## Driving conventions

- Run every mutator command through `$vm drive <project> <transcript> <args...>`, and every project command through `$vm exec`.
- Treat every command as literal. Keep quoted text unchanged.
- Once `.metrics/mutate` exists, a rerun skips mutants it already killed. Pass `--mutate-all` or use a fresh project when a recipe needs every mutant to run.

## Proof and skip reporting

- CLI proof is the transcript: command, stdout, stderr, exit code, worker folders left, processes left, and tracked files changed.
- Record the feature ID with every transcript.
- Report an unreachable path with the attempted command and the unmet precondition.

## Features

- [Mutate a file](./mutate-file.md) covers killing every mutant, a surviving mutant, and listing sites with `--scan`.
- [Worker cleanup](./worker-cleanup.md) covers removing every worker folder, including when the test command leaves a process running in the worker.
- [Control runs](./control-run.md) covers each worker's run of the unmutated tests before its first mutant: a cold worker build doesn't turn mutants into timeouts, and a worker whose unmutated tests fail stops the run.
- [A project's measure script](./project-measure.md) covers a real project's own script that calls mutator, such as bujo's `scripts/measure`.
- [Run time limit](./run-time-limit.md) covers `--baseline-timeout`: a baseline or control run that hangs stops the file with exit code 2 and leaves no test process running.
- [Number options](./number-options.md) covers rejecting `0`, `inf`, `nan`, and text that isn't a number for every number option before any test runs, a huge finite timeout that runs to the end, and the placeholders `--help` shows.
- [Source safety](./source-safety.md) covers an edit saved to the source while mutants run, and a backup left under `target/mutator-backup/` by an older run: deleted when it equals its source, and a stop with exit code `1` when it differs.
