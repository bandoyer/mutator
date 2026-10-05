# A project's measure script

A project can call mutator from its own script, as bujo's `scripts/measure` does (dryer, then crapper, then `mutator --mutate-all src`). The script finds mutator through `MEASURE_TOOLS`, which the helper points at this checkout.

## Sub-features

- `measure-bujo` runs bujo's `scripts/measure` in a clean clone to the end, with no worker folder left.

## How to get to it (user POV)

- From the project root, run the project's script, such as `scripts/measure`.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `project=$($vm project ~/Work/bujo)` gives a fresh clone of bujo's committed files. `T` is the transcript path for the feature.

- **measure-bujo.** Run `$vm exec "$project" "$T" scripts/measure`. Pass: no `Directory not empty` or `Traceback` in stdout or stderr, the mutator score table ends with a `Total` line, and worker folders `(none)`. Record the exit code: `0` when every limit holds.

## Gotchas

- The clone builds bujo from cold, so the first run spends time in `cargo`.
- bujo's mutants all time out today, because each worker builds from cold against a 2 s timeout. mutator counts a timeout as a kill, so don't read the kill count as test strength.
- bujo's `mise.toml` must be trusted in the clone and its workers. The helper sets `MISE_TRUSTED_CONFIG_PATHS` for that.
