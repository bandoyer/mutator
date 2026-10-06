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

- These recipes pass `--no-coverage`, which is faster. With crapper at bandoyer/crapper#47 or later, the fixture also gets coverage without it, once the project has a `.venv` holding coverage.py and pytest. crapper then runs `coverage run --source=.`, writes `target/coverage/python/lcov.info`, and the three sites are `KILLED` as with `--no-coverage`. Without the project's own `.venv`, crapper runs coverage with `python3` and installs coverage.py into it.
- A second run without `--mutate-all`, with nothing changed, keeps the mutants already killed and runs only the baseline. See [Cached kills](./cached-kills.md).
