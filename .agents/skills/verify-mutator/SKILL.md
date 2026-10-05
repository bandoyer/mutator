---
name: verify-mutator
description: Drive this checkout's mutator CLI against a scratch project (a built-in Python fixture or a clone of a real repo) and capture proof (command, stdout, stderr, exit code, worker folders left behind, and tracked files changed). Use to verify any mutator behaviour the way a user would see it, including a project's own measure script that calls mutator.
---

# Verify mutator

mutator is a command-line mutation tester. The only surface is the `./mutator` launcher at the repo root. It mutates source files in worker folders under `<project>/target/mutation-workers`, runs the project's tests in each, prints a score table, and writes `.metrics/mutate/`. A drive never runs against a real project folder: every drive uses a scratch project.

All commands below use the helper at `.agents/skills/verify-mutator/bin/verify-mutator`. Run it from the repo root; call it `vm` for short:

```bash
vm=.agents/skills/verify-mutator/bin/verify-mutator
```

## Launch

There is no server and no build. `./mutator` creates `.venv` on first use. crapper must be checked out next to this repo (`../crapper`).

It is ready when `$vm doctor` prints `doctor: ok`. Teardown is `$vm cleanup <project>` for each scratch project you made.

## Doctor

```bash
$vm doctor
```

It runs no mutants and changes no project file. Its `./mutator --help` call creates `.venv` if it is missing, as Launch says. It fails if the launcher is missing, `./mutator --help` fails, or `../crapper` is missing. On success it prints the Python version, the checkout's commit and branch (and whether `src/`, the launcher, or `pyproject.toml` have uncommitted changes), and the core count. The default worker count is one per core, so record it with your proof.

## Drive

```bash
project=$($vm project fixture)               # Python fixture: demo.py, test_demo.py, pyproject.toml, in its own git repo
project=$($vm project cold-build)            # Cargo fixture whose first build takes 6 s and whose one test can't fail
project=$($vm project ~/Work/bujo)           # or a fresh git clone of a real project (committed files only)
$vm drive "$project" <transcript> --no-coverage demo.py
$vm exec "$project" <transcript> scripts/measure
```

`drive` runs this checkout's `./mutator <args...>` with the project as the working directory, exactly as a user would type it there. `exec` runs any project command the same way, such as a measure script that calls mutator. Both append one block to `<transcript>` and also print it:

- the command, the date and time, the mutator commit, and the exit code
- stdout and stderr
- the folders left in `target/mutation-workers` (`(none)` when cleanup worked)
- tracked files changed in the project (`(none)` when mutator left the source alone)

Both set `MISE_TRUSTED_CONFIG_PATHS` to the project, so a cloned project's `mise.toml` works in the clone and in its workers. Both set `MEASURE_TOOLS` to a folder in the scratch area that links `mutator` to this checkout, and `crapper` and `dryer` to their checkouts next to it. A measure script that reads `MEASURE_TOOLS` therefore runs the mutator under test, not another copy.

The features you can drive, and the end state that proves each one, are in [features/README.md](features/README.md).

## Evidence

- Put transcripts where the caller asks, for example `<run folder>/artifacts/verify/round-1/criterion-1.txt`. Never put them inside the scratch project: cleanup removes it.
- Proof is the transcript: the action (command), what the user saw (stdout, stderr, exit code), and the side effects (worker folders left, tracked files changed). Check all three.
- Use the real user path only: the `./mutator` launcher with real arguments, or the project's own script. Don't import `mutator` in Python, and don't treat `pytest` as proof.
- Exit codes: `0` every executed mutant was killed, `2` the baseline failed, `3` a mutant survived. A Python traceback exits `1`.

## Cleanup

```bash
$vm cleanup "$project"
```

This removes only a scratch folder that `$vm project` created (`$TMPDIR/mutator-verify.*` or `/tmp/mutator-verify.*`), given as the project or the scratch folder itself, after resolving `..` and symlinks. It refuses any other path, including a folder inside the project. `drive` and `exec` likewise refuse a project outside a scratch folder, so a real checkout can't be driven by mistake. Transcripts stay where you wrote them.

## Helpers

`bin/verify-mutator` subcommands: `doctor`, `project fixture | cold-build | <git repo>`, `drive <project> <transcript> [mutator args...]`, `exec <project> <transcript> <command> [args...]`, `cleanup <project>`. Running it with no arguments prints usage.
