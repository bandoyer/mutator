# Coverage reports

A default `mutator` run (no `--no-coverage`, `--use-existing-coverage`, or `--coverage-command`) runs crapper's coverage tool for each language, then reads only the reports those tools wrote in this run. A report they didn't write, such as an earlier run's `coverage/lcov.info`, a hand-made root `coverage.out`, or an old root `target/site/jacoco/jacoco.xml`, is ignored and left on disk. A site on a line no report hits is `UNCOVERED` and doesn't run. `--use-existing-coverage`, `--coverage-command`, and `--scan` still read every report on disk.

## Sub-features

- `coverage-leftover` ignores a leftover `coverage/lcov.info` that marks an untested function as hit, so that function's sites are `UNCOVERED`.
- `coverage-existing` still reads that leftover with `--use-existing-coverage`.

## How to get to it (user POV)

- Run `mutator <file>` in a project that has an earlier report on disk.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`, and `uv` is installed.
- `project=$($vm project fixture)`. `T` is the transcript path for the feature.
- Move the fixture to a `src/` layout, add an untested function `g` on lines 5 and 6, give the project a `.venv` with coverage.py and pytest, and leave an earlier run's report that marks every line as hit:

  ```bash
  $vm exec "$project" "$T" sh -c 'mkdir -p src coverage && git mv demo.py src/demo.py && printf "\n\ndef g():\n    return 2 == 2\n" >>src/demo.py && printf "[tool.pytest.ini_options]\ntestpaths = [\".\"]\npythonpath = [\"src\"]\n" >pyproject.toml && git -c user.name=verify -c user.email=verify@localhost commit -qam src-layout && printf "SF:src/demo.py\nDA:1,1\nDA:2,1\nDA:5,1\nDA:6,1\nend_of_record\n" >coverage/lcov.info && uv venv -q .venv && VIRTUAL_ENV=.venv uv pip install -q coverage pytest'
  ```

- **coverage-leftover.** Run `$vm drive "$project" "$T" --mutate-all src/demo.py`. Pass: exit code `0`, three `KILLED    src/demo.py:2` lines, `UNCOVERED src/demo.py:6 == -> !=`, no `SURVIVED` line, and stderr has `Wrote LCOV report to …/target/coverage/python/lcov.info`. Then `$vm exec "$project" "$T" ls coverage target/coverage/python` lists the leftover `lcov.info` still in `coverage/` and this run's `lcov.info`. The bug shows as `SURVIVED  src/demo.py:6 == -> !=` and exit code `3`.
- **coverage-existing.** After coverage-leftover, run `$vm drive "$project" "$T" --use-existing-coverage --mutate-all src/demo.py`. Pass: exit code `3`, `SURVIVED  src/demo.py:6 == -> !=`, and stderr has no `Wrote LCOV report`.

## Gotchas

- The `src/` layout works with any crapper. Before bandoyer/crapper#47, a file at the project root got no Python coverage (bandoyer/crapper#3).
- Without the project's own `.venv`, crapper runs coverage with `python3` and installs coverage.py into it.
- The run's reproduction loop (`artifacts/reproduce/loop.sh` in the skillflow run for #38) covers the other report kinds: a second Python project, Rust with no coverage tool, Go and Java modules three folders down, and a root `coverage.out` or `jacoco.xml` with no module.
