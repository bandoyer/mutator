# Mutate a file

`mutator <file>` finds mutation sites in the file, runs the project's tests once on the original and once per mutant, prints one line per mutant and a score table, and writes `.metrics/mutate/`. The original file is never changed.

## Sub-features

- `mutate-all-killed` prints `KILLED` for every mutant and exits 0 when the tests catch each one.
- `mutate-survivor` prints `SURVIVED` and exits 3 when a mutant passes the tests.
- `mutate-scan` lists the sites with `--scan` and runs no tests.
- `mutate-own-source` runs each mutant's own source when the project holds a `.pyc` of the original, and leaves that `.pyc` as it was (#82).

## How to get to it (user POV)

- Run `mutator <file>` (or a directory, or nothing for the whole tree) from the project root.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `project=$($vm project fixture)` gives the Python fixture. `T` is the transcript path for the feature.

- **mutate-all-killed.** Run `$vm drive "$project" "$T" --no-coverage demo.py`. Exit code `0`. stdout has three `KILLED    demo.py:2` lines (`1 -> 0` twice, `== -> !=`) and `Total ... 3 0 0 3 100.0%`. stderr has `Wrote .../.metrics/mutate/demo.edn`. Worker folders `(none)`, tracked files changed `(none)`.
- **mutate-survivor.** Run `$vm drive "$project" "$T" --no-coverage --mutate-all --test-command true demo.py`. Exit code `3`, and every mutant line reads `SURVIVED`. Tracked files changed `(none)`.
- **mutate-scan.** Run `$vm drive "$project" "$T" --scan demo.py`. Exit code `0`. stdout lists the three sites on line 2. No `KILLED` or `SURVIVED` lines.

- **mutate-own-source.** In a fresh `project=$($vm project fixture)`, move `demo.py` to a `src/` layout, give the project a `.venv` with pytest, and compile an unchecked-hash `.pyc` of `src/demo.py`. Python uses that kind of `.pyc` whatever the source says:

  ```bash
  $vm exec "$project" "$T" sh -c 'export UV_CACHE_DIR="${UV_CACHE_DIR:-$TMPDIR/uv-cache}" && mkdir -p src && git mv demo.py src/demo.py && printf "[tool.pytest.ini_options]\ntestpaths = [\".\"]\npythonpath = [\"src\"]\n" >pyproject.toml && git add pyproject.toml && git -c user.name=verify -c user.email=verify@localhost commit -qm src-layout && uv venv -q .venv && VIRTUAL_ENV=.venv uv pip install -q pytest && .venv/bin/python -m compileall -q --invalidation-mode unchecked-hash src/demo.py && sha256sum src/__pycache__/demo.*.pyc'
  ```

  Run `$vm drive "$project" "$T" --no-coverage --mutate-all --test-command "$project/.venv/bin/python -m pytest -q -p no:cacheprovider" src/demo.py`, then `$vm exec "$project" "$T" sh -c 'sha256sum src/__pycache__/demo.*.pyc'`. Pass: exit code `0`, three `KILLED    src/demo.py:2` lines, and the same hash as the setup printed. The bug (#82) shows as three `SURVIVED  src/demo.py:2` lines and exit code `3`: every mutant ran the original's bytecode.

## Gotchas

- These recipes pass `--no-coverage`, which is faster. With crapper at bandoyer/crapper#47 or later, the fixture also gets coverage without it, once the project has a `.venv` holding coverage.py and pytest. crapper then runs `coverage run --source=.`, writes `target/coverage/python/lcov.info`, and the three sites are `KILLED` as with `--no-coverage`. Without the project's own `.venv`, crapper runs coverage with `python3` and installs coverage.py into it.
- `mutate-own-source` names the project's own Python in `--test-command`, because a worker has no `.venv` of its own. A worker never sees the project's `__pycache__` (#82). Before the fix, only a nested folder's `__pycache__` was linked into a worker, which is why the recipe uses `src/`: the root's own was already left out. Python trusts a timestamp `.pyc` only when the source's mtime, in whole seconds, and its size match, so that form shows only within a second of a write: the recipe uses an unchecked-hash `.pyc`, which shows it every time.
- A second run without `--mutate-all`, with nothing changed, keeps the mutants already killed and runs only the baseline. See [Cached kills](./cached-kills.md).
