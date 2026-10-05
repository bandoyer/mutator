# Mutate a file

`mutator <file>` finds mutation sites in the file, runs the project's tests once on the original and once per mutant, prints one line per mutant and a score table, and writes `.metrics/mutate/`. The original file is never changed.

## Sub-features

- `mutate-all-killed` prints `KILLED` for every mutant and exits 0 when the tests catch each one.
- `mutate-survivor` prints `SURVIVED` and exits 3 when a mutant passes the tests.
- `mutate-scan` lists the sites with `--scan` and runs no tests.

## How to get to it (user POV)

- Run `mutator <file>` (or a directory, or nothing for the whole tree) from the project root.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `project=$($vm project fixture)` gives the Python fixture. `T` is the transcript path for the feature.

- **mutate-all-killed.** Run `$vm drive "$project" "$T" --no-coverage demo.py`. Exit code `0`. stdout has three `KILLED    demo.py:2` lines (`1 -> 0` twice, `== -> !=`) and `Total ... 3 0 0 3 100.0%`. stderr has `Wrote .../.metrics/mutate/demo.edn`. Worker folders `(none)`, tracked files changed `(none)`.
- **mutate-survivor.** Run `$vm drive "$project" "$T" --no-coverage --mutate-all --test-command true demo.py`. Exit code `3`, and every mutant line reads `SURVIVED`. Tracked files changed `(none)`.
- **mutate-scan.** Run `$vm drive "$project" "$T" --scan demo.py`. Exit code `0`. stdout lists the three sites on line 2. No `KILLED` or `SURVIVED` lines.

## Gotchas

- Pass `--no-coverage` with this fixture. Without it, mutator first runs crapper's Python coverage, which collects nothing for a file at the project root such as `demo.py` (bandoyer/crapper#3). Every site is then `UNCOVERED` and no mutant runs. That coverage run uses `python3`, and crapper installs coverage.py into it when it is missing. [Coverage reports](./coverage-reports.md) moves the fixture to `src/` and gives it a `.venv` first.
- A second run without `--mutate-all` skips mutants already killed and can print nothing new.
