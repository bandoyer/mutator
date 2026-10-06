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

Exit `0` when every executed mutant was killed. Exit `1` on a usage error, when a backup differs from its source, or when the project root is inside `target/mutation-workers`, where another run tests its mutants. Exit `2` when the baseline tests fail, in the project or in a worker, or when a file has no coverage data. Exit `3` when a mutant survives. A failed baseline, or a file with no coverage data, does not rewrite the snapshot.

## Workers

Mutants of one file run at the same time. The default is one worker per core. `--max-workers` sets the cap. The run uses the smaller of that cap, the number of cores, and the number of selected sites. Files are still taken one at a time.

Each worker is a directory under `target/mutation-workers` that holds its own copy of the project, so a test that writes a file changes only its worker's copy. Some things are linked, not copied: `node_modules`, each symlink the project holds, and any folder below the root named like one mutator never shares, such as a nested `.venv` or `target`. A write through one of those still reaches the original ([#70](https://github.com/bandoyer/mutator/issues/70)). At the root, the folders mutator never shares, such as `.venv` and `target`, are left out. `.git`, `.hg`, and `.svn` are left out at every depth ([#8](https://github.com/bandoyer/mutator/issues/8)). A test that finds a checkout beside the project from its own file's path, such as `Path(__file__).resolve().parents[2].parent`, now looks beside the worker's run folder instead. Search each parent folder, as mutator does for crapper. Git run in a worker finds no repository, so a test's `git add` or `git status` can't change the project's index. A test that needs the project's git repository therefore fails its worker's unmutated run, with exit `2`. The worker is removed when that file's mutants finish.

The baseline still runs once, in the real tree, before any worker starts. Each worker then runs the unmutated tests once, without the mutant timeout, before its first mutant. A worker shares no build output with the project, so that run builds it, and mutant runs start as warm as the baseline did. If it fails or times out, the file stops with exit `2` and no mutant is counted. The mutant command then runs inside the worker, in the same directory it would have used in the real tree, so a relative path such as `src` or `./pkg` refers to the worker's copy. Before a non-scan run, a copy an interrupted older run left under `target/mutator-backup/` is deleted when it equals its source. When it differs, mutator stops with exit `1`, names both files, and changes neither.

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

Coverage is generated with crapper's commands unless `--use-existing-coverage` or `--no-coverage` is set. A default run reads only the reports those commands wrote in this run, so an earlier or hand-made report on disk is ignored and left in place. `--use-existing-coverage`, `--coverage-command`, and `--scan` read every report on disk. This needs a crapper checkout with `collect_coverage` (bandoyer/crapper#28). A file that no report lists has no coverage data: its coverage tool is missing or failed, it has no module for the tool to run in, or the reports leave it out. mutator can't tell which of its sites the tests reach, so none of them runs, its snapshot is left as it was, and the run exits `2` after the other files run. stderr names the file. To go on, fix its coverage, leave the file out of the run (for example, `mutator src` in place of the whole tree), or pass `--no-coverage`. A file a report lists with zero hits on every line is measured: its sites are uncovered.

In Python, coverage.py reports a multi-line statement at its first line only. So a site on any line of a statement counts as covered when the statement's first line is hit. This needs the Python that runs mutator to parse the file. If it can't, such as for syntax newer than that Python, only the report's own lines count.

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```
