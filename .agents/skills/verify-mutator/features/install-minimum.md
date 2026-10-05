# Install with the lowest dependencies

`pyproject.toml` declares a lowest version for each dependency, such as `tree-sitter-language-pack>=1.12.5`. A user whose environment holds exactly those versions must get a working mutator. mutator parses through crapper's parser, so it needs the floor crapper needs. The `test (3.11, minimum)` row in `.github/workflows/ci.yml` runs the test suite this way; this recipe drives the CLI.

## Sub-features

- `install-minimum` mutator, run with every direct dependency at its declared lowest version on Python 3.11, lists the mutation sites of a function in each tree-sitter language.

## How to get to it (user POV)

- Install the dependencies at their lowest allowed versions, then run `mutator` in a project.

## Driving it with verify-mutator

Preconditions:

- `uv` is installed. It fetches Python 3.11 if the machine lacks it, and needs network access once.
- `project=$($vm project fixture)`. `T` is the transcript path for the feature.

- **install-minimum.** From the repo root, build the environment outside the checkout:

  ```bash
  env=$(mktemp -d)
  uv venv --python 3.11 "$env/venv"
  VIRTUAL_ENV="$env/venv" uv pip install --resolution lowest-direct -r pyproject.toml
  ```

  Add one function in each other language, record what the environment holds, and run mutator from it:

  ```bash
  $vm exec "$project" "$T" sh -c 'printf "fn a(x: i32) -> i32 { x + 1 }\n" >r.rs && printf "package p\nfunc A(x int) int { return x + 1 }\n" >g.go && printf "function a(x: number) { return x + 1; }\n" >t.ts && printf "class J {\n  int a(int x) { return x + 1; }\n}\n" >J.java && git add -A && git -c user.name=verify -c user.email=verify@localhost commit -qm languages'
  $vm exec "$project" "$T" env VIRTUAL_ENV="$env/venv" uv pip freeze
  $vm exec "$project" "$T" env PYTHONPATH="$PWD/src" "$env/venv/bin/python" -m mutator --scan --no-coverage
  ```

  Pass: the freeze lists `tree-sitter==` and `tree-sitter-language-pack==` at the floors in `pyproject.toml`. The scan exits `0`, prints a `Scan: <n> mutation sites in` line with `n` of 1 or more for each of `demo.py`, `r.rs`, `g.go`, `t.ts`, and `J.java`, and stderr has no `Traceback`. Remove `$env` afterwards. The bug shows as `ImportError: cannot import name 'download'` (language pack 0.7.0) or `TypeError: 'bytes' object is not an instance of 'str'` (1.12.2), exit `1`.

## Gotchas

- This recipe runs mutator through `exec`, not `drive`. `drive` runs `./mutator`, which always uses the checkout's `.venv` with current dependencies, so it can't test the floors.
- `-r pyproject.toml` installs only the dependencies, and `PYTHONPATH` runs this checkout's `src`, so nothing is written inside the checkout. mutator imports crapper from `../crapper`, whose dependencies are the same two packages.
- The language pack downloads each grammar on first parse and caches it per pack version under `~/.cache/tree-sitter-language-pack/v<version>`.
