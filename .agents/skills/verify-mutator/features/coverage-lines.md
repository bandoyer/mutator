# Coverage lines

A Python site is covered when the report hits the first line of the statement that holds it. coverage.py's LCOV report lists a multi-line statement at its first line only, so a site on a later line of a covered statement still runs. mutator splits statements as coverage.py does: from a statement's first token to the end of its logical line. A site in a statement the tests don't run, or one the report leaves out, such as a `# pragma: no cover` line, is `UNCOVERED`.

## Sub-features

- `coverage-continuation` runs the sites on the continuation lines of a covered multi-line `return max(...)`, and `--scan` doesn't mark them `uncovered`.

## How to get to it (user POV)

- Run `mutator <file>` on a Python file whose tested code has statements wrapped over several lines, as black writes them.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`, and `uv` is installed.
- `project=$($vm project fixture)`. `T` is the transcript path for the feature.
- Move the fixture to a `src/` layout, add a function `g` whose `return max(...)` spans lines 6 to 9 with sites on lines 7 and 8, test it, and give the project a `.venv` with coverage.py and pytest:

  ```bash
  $vm exec "$project" "$T" sh -c 'mkdir -p src && git mv demo.py src/demo.py && printf "\n\ndef g(a, b):\n    return max(\n        a + b,\n        a - b,\n    )\n" >>src/demo.py && printf "\n\ndef test_g():\n    from demo import g\n    assert g(3, 1) == 4\n" >>test_demo.py && printf "[tool.pytest.ini_options]\ntestpaths = [\".\"]\npythonpath = [\"src\"]\n" >pyproject.toml && git -c user.name=verify -c user.email=verify@localhost commit -qam continuation && uv venv -q .venv && VIRTUAL_ENV=.venv uv pip install -q coverage pytest'
  ```

- **coverage-continuation.** Run `$vm drive "$project" "$T" --mutate-all src/demo.py`. Pass: exit code `3`, `KILLED    src/demo.py:7 + -> -`, `SURVIVED  src/demo.py:8 - -> +`, and no `UNCOVERED` line. The bug shows as `UNCOVERED src/demo.py:7 + -> -`, `UNCOVERED src/demo.py:8 - -> +`, and exit code `0`. Then run `$vm drive "$project" "$T" --scan src/demo.py`. Pass: exit code `0`, and the lines for `src/demo.py:7` and `src/demo.py:8` don't end in `uncovered`.

## Gotchas

- The run's reproduction loop (`artifacts/reproduce/loop.sh` in the skillflow run for #61, `continuation-coverage`) covers the other forms: a call's arguments, `return any(...)` over a generator, a multi-line `if` test, and a multi-line decorator, beside controls for an untested `if` body, an untested function, and a `# pragma: no cover` line.
