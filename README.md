# mutator

Mutation testing for Clojure, Java, Go, TypeScript, Rust, and Python. One run detects the language of each source file, applies that language's mutation rules, and writes the snapshot [uml-viewer](https://github.com/unclebob/uml-viewer) already reads.

Clojure follows [clj-mutate](https://github.com/unclebob/clj-mutate). Go follows [mutate4go](https://github.com/unclebob/mutate4go). Java follows [mutate4java](https://github.com/unclebob/mutate4java). TypeScript, Rust, and Python use the same decisions as Java, spelled in that language. Function names and namespaces come from [crapper](https://github.com/unclebob/crapper), which is what uml-viewer joins to `.metrics/crap.edn`.

A mutant is killed when the tests fail or time out. It survives when the tests still pass. A site on a line the coverage report does not hit is uncovered and is not run. The score uml-viewer paints is killed / (killed + survived).

## Run

```bash
./mutator
```

The first run creates `.venv` and installs the tool. crapper has to be checked out next to this directory (`../crapper`), because that is where the namespace and function names come from. From a project root the tool walks the tree, skips `test`, `spec`, `vendor`, `node_modules`, and `target`, and writes `.metrics/mutate/<namespace>.edn`.

The first run of a tree executes every covered mutant. Start with the file you are working on.

```bash
./mutator src/demo/core.clj          # one file, or a directory
./mutator --scan src/demo           # list sites, do not run tests
./mutator --changed                 # git additions and edits
./mutator --mutate-all              # rerun killed mutants too
./mutator --no-coverage             # do not skip uncovered lines
./mutator --use-existing-coverage   # read reports already on disk
./mutator --test-command "pytest -q tests/test_demo.py"
./mutator --max-workers 3 src/demo/core.clj
```

Once a snapshot exists, the next run is differential. It reruns survivors. It keeps a killed mutant from the snapshot only when nothing the kill depends on has changed:

- every file git lists for the project, tracked or untracked but not ignored, apart from `.metrics/` and mutator's own `target/mutation-workers/` and `target/mutator-backup/`;
- the test command and the directory it runs in;
- `--timeout-factor`.

So any edit to a test, a source file, a config file, or a lockfile reruns every killed mutant. Outside a git repository, nothing is kept. Even when every kill is kept, the baseline runs, so tests that fail now stop the file with exit `2`. Files git ignores, such as `.venv` or `node_modules`, and toolchain versions are not part of the check ([#56](https://github.com/bandoyer/mutator/issues/56)). `--since-last-run` is that same selection. `--mutate-all` is unchanged: it reruns every covered site, whatever the snapshot holds.

Exit `0` when every executed mutant was killed. Exit `1` on a usage error, when a backup differs from its source, or when the project root is inside `target/mutation-workers`, where another run tests its mutants. Exit `2` when the baseline tests fail, in the project or in a worker, when a file has no coverage data, or when a coverage run fails but writes a report. Exit `3` when a mutant survives. A failed baseline, a file with no coverage data, or a failed coverage run's report does not rewrite the snapshot.

## Workers

The worker and cache behavior below describes the default `native` backend.
For the optional Python backend, see [Python with mutmut](#python-with-mutmut).

Mutants of one file run at the same time. The default is one worker per core. `--max-workers` sets the cap. The run uses the smaller of that cap, the number of cores, and the number of selected sites. Files are still taken one at a time.

Each worker is a directory under `target/mutation-workers` that holds its own copy of the project, so a test that writes a file changes only its worker's copy. Some things are linked, not copied: `node_modules`, each symlink the project holds, and any folder below the root named like one mutator never shares, such as a nested `.venv` or `target`. A write through one of those still reaches the original ([#70](https://github.com/bandoyer/mutator/issues/70)). At the root, the folders mutator never shares, such as `.venv` and `target`, are left out. `.git`, `.hg`, and `.svn` are left out at every depth ([#8](https://github.com/bandoyer/mutator/issues/8)), and so is `__pycache__`. A worker compiles the project's Python from source. Before each mutant runs, mutator removes that file's `.pyc` from the worker, and a worker's commands get no `PYTHONPYCACHEPREFIX`, so any bytecode they write stays beside the sources, where mutator removes it. So a mutant always runs its own code, never a cached `.pyc` of the original or of another mutant, unless the test command sets its own `-X pycache_prefix`. Python can trust a stale `.pyc` when a same-size mutant is written in the same second as the source it was compiled from ([#82](https://github.com/bandoyer/mutator/issues/82)). A test that finds a checkout beside the project from its own file's path, such as `Path(__file__).resolve().parents[2].parent`, now looks beside the worker's run folder instead. Search each parent folder, as mutator does for crapper. Git run in a worker finds no repository, so a test's `git add` or `git status` can't change the project's index. A test that needs the project's git repository therefore fails its worker's unmutated run, with exit `2`. The worker is removed when that file's mutants finish.

The baseline still runs once, in the real tree, before any worker starts. Each worker then runs the unmutated tests once, without the mutant timeout, before its first mutant. A worker shares no build output with the project, so that run builds it, and mutant runs start as warm as the baseline did. If it fails or times out, the file stops with exit `2` and no mutant is counted. The mutant command then runs inside the worker, in the same directory it would have used in the real tree, so a relative path such as `src` or `./pkg` refers to the worker's copy. Before a non-scan run, a copy an interrupted older run left under `target/mutator-backup/` is deleted when it equals its source. When it differs, mutator stops with exit `1`, names both files, and changes neither.

Each worker's commands get a temp folder of the worker's own as `TMPDIR`, outside the project, and it is removed with the worker. Workers' pytest sessions therefore never share a basetemp root, where each session prunes the others' folders.

## Snapshot

uml-viewer loads every `*.edn` file under `.metrics/mutate`. Each file is one namespace. A Java file with an inner class, or a Go file with a function and a method, therefore writes more than one file. Two Go files in the same package share one snapshot, and rerunning one of them keeps the other's forms.

```clojure
{:version 1
 :source "src/demo/board.py"
 :namespace "demo.board.Board"
 :outcomes {"[\"src/demo/board.py\",\"demo.board.Board\",\"defn/place\",10,11,\">\",\">=\"]" :survived}
 :forms [{:id "defn/place"
          :kind "defn"
          :file "src/demo/board.py"
          :line 2
          :end-line 4
          :hash "…"
          :context "…"
          :killed 1
          :survived 1
          :uncovered 0
          :sites 2}
         {:id "defn-/_hide"
          :kind "defn-"
          :killed 0
          :survived 0
          :uncovered 1
          :sites 1}]}
```

`:namespace` is the class key crapper uses. The form id is what the viewer splits:

| Form id | Operation | Private |
| --- | --- | --- |
| `defn/place` | `place` | no |
| `defn-/hide` | `hide` | yes |

`:context` is a digest of the test context the function's outcomes came from: the project's files, the test command and its directory, and `--timeout-factor`. A differential run keeps a kill only while `:hash` and `:context` both match. It is `nil` outside a git repository.

`:sites` is every mutation in the function, including uncovered ones. The viewer prints `---no mutation sites---` when that count is zero.

| Language | `:namespace` | `:name` |
| --- | --- | --- |
| Clojure | the `ns` | the `defn` / `defn-` name |
| Java | `package.Class`, or `package.Outer.Inner` | the method name |
| Go | the package import path, or `import/path.Receiver` | the function or method name |
| TypeScript | the dotted module path, or `module.Class` | the function or method name |
| Rust | `crate::module`, or `crate::module::Type` | the function or method name |
| Python | the dotted module path, or `module.Class` | the function or method name |

`defn-` is Clojure's private form. Elsewhere a private method, an unexported Go function, a Python name that starts with `_`, a Rust function that is not `pub`, or a TypeScript `private` / `#` name uses `defn-/`.

## What each language mutates

Tokens inside strings, comments, and quoted Clojure forms are left alone. A replacement that does not compile fails the test run and counts as killed.

| Language | Mutations |
| --- | --- |
| Clojure | clj-mutate's head symbols: `+`/`-`, `*` to `/`, `inc`/`dec`, comparisons, `=`/`not=`, `if`/`if-not`, `when`/`when-not`, `and`/`or`, and the seq, predicate, and coercion pairs. `true`/`false` and `0`/`1` anywhere. |
| Java | `true`/`false`, `==`/`!=`, `<`/`<=`, `>`/`>=`, `+`/`-`, `*`/`/`, `&&`/`||`, delete `!` and unary `-`, `0`/`1`. Constructors are not entries, matching crapper. |
| Go | mutate4go: `+`/`-`, `*` to `/` only, comparisons, `==`/`!=`, `true`/`false`, `&&`/`||`, `0`/`1`. |
| TypeScript | the Java set, plus `===`/`!==`, `??` to `||`, and `?.` to `.`. A call or index drops `?.` (`a?.()` becomes `a()`). `.js`, `.jsx`, `.mjs`, and `.cjs` use these rules. |
| Rust | the Java set (`&&`/`||`, `!`, unary `-`). |
| Python | the Java set, spelled `and`/`or`, `not`, and `True`/`False`. |

## Tests and coverage

The baseline command has to pass before any mutant runs. A mutant's timeout is ten times that baseline, and at least two seconds. `--timeout-factor` changes the multiple. The baseline and each worker's unmutated run have their own limit, 600 seconds unless `--baseline-timeout` says otherwise. A run that reaches it stops the file with exit `2`, and the message says which run timed out. `--test-command` replaces the default. Defaults:

| Language | Command | Where |
| --- | --- | --- |
| Clojure | `clj -M:spec --tag ~no-mutate` when Speclj is on the classpath, otherwise `clj -M:test` or `bb spec` | nearest `deps.edn` or `bb.edn` |
| Java | `mvn -q test -DexcludeTags=no-mutate` | nearest `pom.xml` |
| Go | `go test -count=1` of the file's package | nearest `go.mod` |
| TypeScript | `npm test` | nearest `package.json` |
| Rust | `cargo test` | nearest `Cargo.toml` |
| Python | the project's `.venv` or `venv` Python running `pytest`, or `unittest discover` | nearest project file |

Coverage is generated with crapper's commands unless `--use-existing-coverage` or `--no-coverage` is set. A default run reads only the reports those commands wrote in this run, so an earlier or hand-made report on disk is ignored and left in place. `--use-existing-coverage`, `--coverage-command`, and `--scan` read every report on disk. This needs a crapper checkout with `collect_coverage` (bandoyer/crapper#28). A file that no report lists has no coverage data: its coverage tool is missing or failed, it has no module for the tool to run in, or the reports leave it out. mutator can't tell which of its sites the tests reach, so none of them runs, its snapshot is left as it was, and the run exits `2` after the other files run. stderr names the file. To go on, fix its coverage, leave the file out of the run (for example, `mutator src` in place of the whole tree), or pass `--no-coverage`. A file a report lists with zero hits on every line is measured: its sites are uncovered. A coverage run can fail and still write a report, such as coverage.py's report from tests that fail only under coverage. crapper then returns that report with the run's exit status (bandoyer/crapper#55). mutator names the report and stops before any mutant runs, as it does when `--coverage-command` fails, but with exit `2` (a failed `--coverage-command` exits with that command's status). Otherwise sites that only the failed run would reach would read as uncovered. Fix the coverage run, or pass `--no-coverage`. With a crapper older than that, reports carry no status, and mutator scores them as before.

In Python, coverage.py reports a multi-line statement at its first line only. So a site on any line of a statement counts as covered when the statement's first line is hit. This needs the Python that runs mutator to parse the file. If it can't, such as for syntax newer than that Python, only the report's own lines count.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

## Python with mutmut

`--python-backend mutmut` selects an experimental adapter for **mutmut 3.8.0**.
Mutmut generates the Python mutations and selects the pytest tests that call
each mutated function. Its forked workers reuse pytest's imports. Other
languages still use the native engine.

Install the extra in mutator's environment, and install the same dependencies
in the project's Python if it has a separate environment:

```bash
uv pip install --python .venv/bin/python -e '.[python]'
# From the project being tested:
uv pip install --python .venv/bin/python 'mutmut==3.8.0' 'tomli-w>=1.0'
/path/to/mutator/mutator --python-backend mutmut --max-workers 4 src/demo.py
```

This backend is for pytest tests that import the source in the test process.
It needs fork support and pytest configuration in `pyproject.toml`. If the
project has a `.venv` or `venv`, that interpreter runs the backend. Otherwise
it uses mutator's interpreter. `--test-command` accepts `python -m pytest`
with arguments. Shell wrappers are rejected. Run mutator itself inside your
sandbox. Use pytest's `testpaths` or mutmut's
`pytest_add_cli_args_test_selection` to select a suite; a test path appended
to `--test-command` also runs for every mutant and reduces test selection's
benefit.

Existing `tool.mutmut` settings other than those two pytest argument lists
are refused, so an incompatible setting is never silently discarded.

The adapter makes a disposable copy under `target/mutation-workers` and keeps
links into the project inside that copy. Mutmut's parallel processes share
the copied filesystem, so tests must isolate their own files. The original
source and configuration stay untouched. Process isolation and a disposable
copy do not restrict writes through external paths or external symlinks.

Tests that execute the CLI through another interpreter can fail the clean
run because the generated code imports mutmut. Calls in a subprocess also
do not supply mutmut's in-process test associations. Use the native backend
for such suites. The adapter stops with exit 2 and preserves prior Python
snapshots when setup, clean tests, or result translation fails. Interrupts,
pytest internal errors, crashes, and incomplete results do not count as kills.
Mutant timeouts count as kills, matching the native engine.

`--lines` selects mutation locations and `--scan` lists mutmut's sites without
running tests or writing metrics. Functions without associated tests are
uncovered. `--no-coverage` instead tries the full suite for those functions.
If no association can be established for any generated function, mutmut
refuses the run. External coverage options and `--since-last-run` require
the native backend.

Every mutmut invocation starts fresh. This first adapter does not reuse
mutmut's cache. Its snapshot mutation IDs include the backend and pinned
version, so native results cannot be reused as mutmut results. Mutmut's
operators differ from the native operators; compare individual mutations
when assessing a change in score. A line-filtered snapshot contains only
mutmut sites generated for that selection.

`--timeout-factor` applies to mutmut's estimate for the selected tests.
For this backend, `--baseline-timeout` bounds the **entire backend command**,
including preparation and mutants; the default is 600 seconds. Reaching it
stops with exit 2. `--memory-limit` applies to the backend process and is
inherited by its children.

The adapter uses private mutmut generation and result APIs, isolated in
`mutmut_bridge.py`. Other mutmut versions are refused. Upgrade the pin only
with the integration tests and a real project trial. See
[the initial skillflow experiment](benchmarks/python-mutmut.md).
