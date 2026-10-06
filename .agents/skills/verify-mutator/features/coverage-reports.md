# Coverage reports

A default `mutator` run (no `--no-coverage`, `--use-existing-coverage`, or `--coverage-command`) runs crapper's coverage tool for each language, then reads only the reports those tools wrote in this run. A report they didn't write, such as an earlier run's `coverage/lcov.info`, a hand-made root `coverage.out`, or an old root `target/site/jacoco/jacoco.xml`, is ignored and left on disk. A site on a line no report hits is `UNCOVERED` and doesn't run. When a coverage run fails but still writes a report (crapper returns its exit status in `Report.code`, crapper#55), mutator names that report and stops before any mutant runs, as it does for a failed `--coverage-command`, but with exit `2` (a failed `--coverage-command` exits with that command's status). `--use-existing-coverage`, `--coverage-command`, and `--scan` still read every report on disk.

When no report lists a source file, coverage didn't measure it: its tool is missing or failed, it has no module, or the reports leave it out. A run that reads coverage then stops that file. None of its sites runs or prints, its snapshot is not written, stderr names it and how to go on, and the run exits `2`. Other files still run. A file a report does list, even with zero hits on every line, is measured: its sites are `UNCOVERED` as usual.

## Sub-features

- `coverage-leftover` ignores a leftover `coverage/lcov.info` that marks an untested function as hit, so that function's sites are `UNCOVERED`.
- `coverage-existing` still reads that leftover with `--use-existing-coverage`.
- `coverage-unmeasured` stops a file whose project has no coverage.py, with exit `2`, and `--no-coverage` still runs its sites.
- `coverage-failed-run` stops with exit `2`, and runs no mutant, when a coverage run fails but still writes a report (#66).

## How to get to it (user POV)

- Run `mutator <file>` in a project that has an earlier report on disk.
- Run `mutator <file>` in a project whose coverage tool is missing.

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
- **coverage-unmeasured.** Use a fresh fixture. Move it to a `src/` layout, and give it a `.venv` with pytest but not coverage.py. uv makes the venv without pip, so crapper can't install coverage.py:

  ```bash
  $vm exec "$project" "$T" sh -c 'mkdir -p src && git mv demo.py src/demo.py && printf "[tool.pytest.ini_options]\ntestpaths = [\".\"]\npythonpath = [\"src\"]\n" >pyproject.toml && git -c user.name=verify -c user.email=verify@localhost commit -qam src-layout && uv venv -q .venv && VIRTUAL_ENV=.venv uv pip install -q pytest'
  ```

  Run `$vm drive "$project" "$T" --mutate-all src/demo.py`, then `$vm exec "$project" "$T" find . -path ./.venv -prune -o -name '*.edn' -print`. Pass: exit code `2`, stdout has no `KILLED`, `SURVIVED`, or `UNCOVERED` line, stderr has `No coverage data for src/demo.py`, says none of its 3 sites ran, and names `--no-coverage`, and `find` prints nothing. The bug shows as exit code `0`, three `UNCOVERED src/demo.py:2` lines, and `./.metrics/mutate/demo.edn`. Then run `$vm drive "$project" "$T" --no-coverage --mutate-all src/demo.py`. Pass: exit code `0` and three `KILLED    src/demo.py:2` lines.

- **coverage-failed-run.** Needs a crapper with `Report.code` (bandoyer/crapper cf6b041 or later) in the checkout's `.venv` or at `../crapper`. In a fresh project, `project=$($vm project fixture)`, move to the `src/` layout, and make the only test fail under coverage.py alone:

  ```bash
  $vm exec "$project" "$T" sh -c 'mkdir -p src && git mv demo.py src/demo.py && printf "import sys\n\nfrom demo import f\n\n\ndef test_f():\n    assert \"coverage\" not in sys.modules, \"fails only under coverage.py\"\n    assert f() is True\n" >test_demo.py && printf "[tool.pytest.ini_options]\ntestpaths = [\".\"]\npythonpath = [\"src\"]\n" >pyproject.toml && git -c user.name=verify -c user.email=verify@localhost commit -qam fails-under-coverage && uv venv -q .venv && VIRTUAL_ENV=.venv uv pip install -q coverage pytest'
  ```

  Run `$vm drive "$project" "$T" --mutate-all src/demo.py`, then `$vm exec "$project" "$T" find . -path ./.venv -prune -o -name '*.edn' -print`. Pass: exit code `2`. stdout has no `KILLED`, `SURVIVED`, or `UNCOVERED` line. stderr has crapper's `Python coverage exited 1 in …, but wrote …/target/coverage/python/lcov.info`, then `Coverage report from a failed run: …/target/coverage/python/lcov.info (exited 1 in …).`, and a `No mutant ran:` line that names `--no-coverage`. `find` prints nothing. The bug (#66) shows as exit code `0`, three `UNCOVERED src/demo.py:2` lines, and `./.metrics/mutate/demo.edn`. Then run `$vm drive "$project" "$T" --no-coverage --mutate-all src/demo.py`. Pass: exit code `0` and three `KILLED    src/demo.py:2` lines.

## Gotchas

- The `src/` layout works with any crapper. Before bandoyer/crapper#47, a file at the project root got no Python coverage (bandoyer/crapper#3).
- Without the project's own `.venv`, crapper runs coverage with `python3` and installs coverage.py into it.
- Two skillflow runs' reproduction loops (`artifacts/reproduce/loop.sh`) cover the other report kinds. The run for #38 has a second Python project, Go and Java modules three folders down, and a Rust package, a Go file, and a Java file whose leftover report is not read. The run for #12 (`coverage-provenance`) has a file coverage didn't measure in each language and coverage mode, beside a measured package.
