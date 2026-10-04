# Mutator ownership review

Reviewed on October 4, 2026, at commit `c57f03879a08d2afe8c7e044e86c80bb164afd30` in `bandoyer/mutator`, forked from `unclebob/mutator`. The sibling crapper dependency was reviewed at `9f1bead298b5a9d576bdd6319289fcf426e5b18a`.

Mutator needs correctness and source-protection fixes before its score is used as a release check. I reproduced both false kills and false survivors, stale successful scores, and writes that can replace project data. Its basic decomposition is workable. The first ownership work should make test execution and result reuse trustworthy, then reduce repeated analysis and snapshot work.

This is a full repository review at the recorded commit, including worker construction, process execution, source extraction, coverage selection, incremental results, tests, packaging, and documentation. This PR changes only this report. One initial review and one evidence/report check were used. No implementation repair pass was needed.

## Verification and limits

- `.venv/bin/python -m pytest -q`: **72 passed in 1.20 seconds**.
- Environment: Linux, Python 3.14.8, pytest 9.1.1, tree-sitter 0.26.0, tree-sitter-language-pack 1.21.0. Real Node probes used Node 26.7.0. Pytest was added to the existing virtual environment.
- The repository contains 14 production Python files with 3,197 lines and eight test files with 1,968 lines. All production modules and tracked support files were inspected; test inspection focused on assertions and execution boundaries, alongside the full suite.
- Ran actual Python and Node mutation commands in temporary projects. Also exercised worker writes, backup restoration, function ownership, deleted-source snapshots, and snapshot write counts.
- No production fixes were applied. Maven, Go, Cargo, and Clojure mutation pipelines were not exercised end to end. Their source extraction and command construction are covered to varying degrees by unit tests, not by a demonstrated full build-and-mutate cycle here.

Priorities: **P1** can corrupt user data or invalidate mutation results. **P2** is a narrower correctness or compatibility problem. Advice is separated from required corrections.

## Act on

These findings have reproducible consequences for source safety or the reported score.

### M1. Verify the unmutated worker before counting test failures as kills: P1

A successful baseline in the real project does not prove the worker can run the same command. Workers omit `.venv`, `.git`, and build directories. A supplied relative interpreter path therefore succeeds during baseline and fails in every worker.

Reproduction:

```python
# demo.py
def f():
    return 1
```

With an existing project interpreter, run:

```sh
mutator --no-coverage --max-workers 1 \
  --test-command './.venv/bin/python -c "import demo; assert demo.f() in (0, 1)"'
```

The assertion deliberately accepts both the original and the mutant. Observed result: `1 -> 0` was **KILLED**, the score was **100%**, and the exit code was **0**. The worker had no `.venv/bin/python`. A shell startup error became a mutation kill.

Evidence: [real-tree baseline](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L191), [SKIP_LINK](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L25), [_execute](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L296), and [CommandRunner](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/runner.py#L73). The default Python planner uses an absolute interpreter and avoids this particular path error; custom commands remain affected.

Smallest repair: run an unmutated control in each distinct worker environment before mutations. Abort with an execution error if that control fails. Preserve command output and distinguish launch/infrastructure failures from valid test kills. Keep compilation-error policy explicit. Add this weak-assertion example so a missing runner cannot make a test pass.

### M2. Validate the baseline and invalidate cached kills when the test context changes: P1

The cache checks the source function hash and mutation identity. It does not include tests, test command, dependencies, or tool configuration. Worse, `_apply_selected` returns before baseline validation when all sites are cached as killed.

Reproduction used a real absolute Python command asserting `demo.f() == 1`. The first run correctly killed `1 -> 0`. A second run with identical source and `--test-command false` still exited **0**, printed the old **100%** score, and rewrote the snapshot. It never ran the now-failing baseline.

Evidence: [_select](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L61), [_carry_forward](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L87), [_apply_selected](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L209), and [form_digests](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/functions.py#L179). The README requires a passing baseline and describes cached results as current outcomes.

Smallest repair: validate each test plan even when no mutant is selected. Add a test-context fingerprint to cache validity, starting with the command, test sources, relevant manifests/lockfiles, and tool version. If that context cannot be established, require a full rerun before claiming a current complete score. Test deleted assertions and changed commands with unchanged production source.

### M3. Remove unconditional writes back to the original source: P1

Two recovery paths can replace newer edits:

- `_restore_if_dirty` writes the initially captured bytes whenever the real source differs at the end of the run. With private worker mutation, that difference can be a user's edit made while tests run.
- `restore_backups` replaces current source with any legacy backup under `target/mutator-backup`, without checking whether the current file is still the interrupted mutant.

Temporary-file probes replaced `new user edit` with `original` in the first case and with `stale backup` in the second. Both then discard the newer content; the backup path also deletes its recovery input.

Evidence: [restore_backups](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L17), [_restore_if_dirty](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L258), and [final result write and restore](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L313).

Smallest repair: remove end-of-run restoration of the real source in the overlay execution path. For legacy recovery, retain the backup and diagnose a conflict unless the current content matches a recorded interrupted mutation or an explicit recovery request authorizes replacement. Test concurrent edits and stale backups, not only successful restoration of known mutated bytes.

### M4. Make worker writes independent of the project tree: P1

Only the selected source, selected JavaScript importers, and some root configuration files are copied. Other files and whole directories are writable symlinks into the original project.

A direct reproduction created `fixture.txt`, built a worker, and wrote to `worker/fixture.txt`. The original became `overwritten by worker`. Tests that update snapshots, generate code, format source, or write into an existing data directory can therefore change real project files. Multiple workers can also race on those shared paths. The current private-source test does not verify these writes.

Evidence: [symlink](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L89), [_link_children](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L122), [_overlay](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L258), and [worker tests](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/tests/test_engine.py#L151). This contradicts the README's unqualified claim that original files stay unchanged.

Smallest safe repair: use independent copies or filesystem copy-on-write clones for writable project content. Keep any intentionally shared dependency cache read-only or governed by a documented supported workflow. Validate two workers writing the same fixture path and verify that neither the original nor the other worker changes. Measure the copying/build cost before choosing the final storage strategy.

### M5. Ensure Node package aliases load the mutant: P1

The importer graph recognizes only relative string specifiers. Node resolves symlinked test modules to their original location, so package imports can bypass the worker copy.

Reproduction:

```json
{"type":"module","imports":{"#demo":"./src/demo.mjs"}}
```

```javascript
// src/demo.mjs
export function f() { return 1; }
// tests/test.mjs
import { f } from "#demo";
if (f() !== 1) process.exit(1);
```

Run with `--no-coverage --max-workers 1 --test-command 'node tests/test.mjs'`. Observed: `1 -> 0` **SURVIVED**, exit **3**. The test asserts the exact value, but loads the original module through the real test path.

Evidence: [relative-specifier regex](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L135), [importers_of](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L202), and [create_workers](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L270). This is a supported [Node package import mechanism](https://nodejs.org/api/packages.html#subpath-imports), not an arbitrary dynamic-loader example.

Smallest repair: copying all relevant project modules and tests avoids real-path escape without recreating Node's resolver in a regex. If retaining selective copying, resolve package imports and project aliases correctly or reject unsupported resolution before reporting a score. Add both this package-import fixture and the existing relative-import fixture to end-to-end tests.

### M6. Give all function extractors byte spans before assigning mutation ownership: P2

For this source, both constant mutations are assigned to `a`:

```java
class A { int a(){return 0;} int b(){return 1;} }
```

Observed site owners were `[('0', 'a'), ('1', 'a')]`. Crapper provides byte positions for TypeScript functions but leaves the default `-1` for the other language extractors. Mutator falls back to line ranges. Distinct methods on one line tie, and the first match wins.

Evidence: [_contains](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/functions.py#L118), [_tightness](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/functions.py#L128), and [_owner](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/functions.py#L136). The shared definition is in [crapper's Function model](https://github.com/bandoyer/crapper/blob/9f1bead298b5a9d576bdd6319289fcf426e5b18a/src/crapper/model.py#L5).

Smallest repair: record UTF-8 byte ranges in every crapper extractor and require containment by bytes for site ownership. Keep overload grouping as a separate reporting decision. Test adjacent methods on one line and nested owners in each supported grammar.

### M7. Prune results for deleted and renamed files during a full run: P2

`write_results` removes old forms for a file only when that file is passed in again. CLI discovery excludes deleted files, and no full-run reconciliation removes them. Renaming a file leaves its old forms alongside the new ones.

A temporary project seeded a snapshot for nonexistent `deleted.py`, then ran a full mutation pass over the remaining source. `.metrics/mutate/deleted.edn` still existed afterward. The viewer reads every snapshot and can show obsolete results indefinitely.

Evidence: [write_results](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/metrics.py#L283), [_mutate_files](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/cli.py#L529), and [empty selection handling](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/cli.py#L563). The test that calls `write_results(root, file, [], {})` proves the low-level removal function works, not that the CLI invokes it for deleted files.

Smallest repair: reconcile recorded source paths against the complete discovered source set after a successful full run. For partial runs, remove only explicitly known deleted/renamed sources. Add full CLI tests for deletion, rename, and removal of the final source file.

### M8. Carry coverage provenance and failure state through the crapper integration: P1

Mutator inherits crapper's confirmed coverage faults, then uses those results to decide which mutants do not run:

- A failed `npm run coverage` can leave an old `coverage/lcov.info` active.
- Flat Python `demo.py` projects receive `--source=demo.py`; the real coverage.py run reported no collected data despite a passing test.
- Reports from separate packages using the same `SF:src/core.py` path are merged without package identity.

Evidence: [_prepare_coverage](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/cli.py#L433), [covered_lines](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/coverage.py#L212), and [_covered](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L53). See C1–C3 in [crapper's ownership report](https://github.com/bandoyer/crapper/blob/review/ownership-2026-10-04/OWNERSHIP_REVIEW.md) for the reproduced coverage results and proposed fixes.

The per-language crapper collector returns `0` even after tool failures, so mutator cannot distinguish a failed collection from a valid report with uncovered lines. A file with no usable coverage can have every site skipped and the overall command can still return `0`, as the current CLI contract permits.

Smallest repair: expose structured collection status and source ownership from crapper. Keep valid zero-hit coverage distinct from failed or missing measurement. Refuse a successful completeness claim when requested coverage collection failed. Verify this at the mutator CLI boundary, not only in crapper unit tests.

## Consider

These are maintenance, distribution, and efficiency improvements with explicit tradeoffs.

### M9. Stop rereading and rewriting every snapshot for each source file

`load_history` scans every EDN snapshot. `write_results` scans them again, and `touched` includes every retained namespace, including unrelated ones. Each file therefore rewrites all accumulated namespaces and advances their `tested-at` values even when their tests were not rerun.

Measured with 20 calls to `write_results`, each adding one file with one unique namespace: **210 namespace writes**. An update to one file also reports unrelated namespace files as written.

Evidence: [load_history](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/metrics.py#L106), [_read_snapshots](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/metrics.py#L182), [write_results](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/metrics.py#L283), and [tested-at generation](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/metrics.py#L137).

Load and index snapshots once per invocation. Touch only incoming namespaces and previous namespaces belonging to the selected source. Preserve unrelated bytes and timestamps. Write each changed namespace atomically. Add a result-schema field that distinguishes newly tested outcomes from carried-forward ones rather than labeling both with a new test timestamp.

### M10. Reuse analysis within a run and bound worker cost

A normal mutation pass calls `project_functions` four times per source: site ownership, initial digests, final spans, and final digests. Raw mutation discovery also parses the file. `_owner` scans all functions for every site, and digest/header helpers repeatedly split the whole source into lines.

Coverage is loaded afresh for every selected file. `create_workers` rebuilds the entire JavaScript relative-import graph for every file, even when the selected file is Python or another non-JavaScript language.

Evidence: [mutate_file](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L263), [_forms](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/engine.py#L101), [sites_in_file](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/functions.py#L143), [covered_lines](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/coverage.py#L212), and [create_workers](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/workers.py#L270). These are source-traced repeated operations; their share of a real build's runtime was not profiled.

Parse and split each source once, cache coverage and the importer graph for the invocation, and assign sites with ordered byte intervals. Benchmark before adding a persistent cache. Compilation and test execution may still dominate.

The default worker count uses every CPU. Each worker can launch another parallel build or test runner, and skipped build directories force independent build work. Start with a conservative documented cap, retain `--max-workers`, and measure both peak memory and wall time on a representative Maven/Cargo project before increasing concurrency. Add an explicit baseline timeout; baselines and custom coverage currently have no timeout.

### M11. Make the crapper dependency installable and versioned

`pyproject.toml` does not declare crapper. `ensure_crapper` either imports any installed version or finds a sibling checkout by walking from its own source file. This works for the documented checkout layout and did work in this review. A normal wheel installation has no guaranteed sibling source tree and no version contract.

Evidence: [dependencies](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/pyproject.toml#L9) and [sibling discovery](https://github.com/bandoyer/mutator/blob/c57f03879a08d2afe8c7e044e86c80bb164afd30/src/mutator/crapper_link.py#L13). All three manifests also allow tree-sitter-language-pack 0.7.0 even though crapper imports an API named `download` that is absent from that wheel.

Choose a releaseable crapper library dependency or a deliberately bundled shared package. Publish a stable extraction/coverage interface instead of importing internal helpers such as `crapper.coverage._DOCTYPE`. Test isolated wheel installation outside the development checkout and the actual minimum parser version. Keep this as a small packaging change before considering a monorepo.

### M12. Remove obsolete code and simplify the parts that make changes hard

- `_backup` and `_drop_bytecode` remain from the old in-place mutation path. No production caller uses them; boundary tests call them directly. Retire them after settling legacy recovery under M3. Their tests do not justify keeping unused production code.
- `bugs/python-tests-run-with-mutators-virtualenv.md` describes an implementation already replaced by `_project_interpreter`. Mark it resolved and retain it as history, or remove it from active bug documentation.
- Keep the process runner, model, and mutation rules separate. Reduce long argument chains in the worker functions with a small immutable execution context only where that makes inputs clearer.
- Use a standard CLI parser while preserving documented exit codes and conflicts. Validate finite timeout values. `float('inf')` and `float('nan')` pass the current positivity check.
- The EDN subset is small and matches the current snapshot shape. Retain it unless a proven library materially reduces maintenance; do not add a large serialization dependency just for style.

### M13. Add tests that exercise real language execution boundaries

The suite has useful real Python subprocess checks, process-timeout checks, and a conditional real Node relative-import test. Several engine tests use a runner that reads a file and executes it directly, bypassing import resolution and build configuration. That explains why green unit tests do not establish worker equivalence.

Add a small supported-language matrix: an unmutated worker passes, a known behavioral mutant fails, a deliberately weak assertion allows a survivor, source bytes remain unchanged, and coverage points at the exact source tested. Use tiny Maven, Go, Cargo, Clojure, Node, and Python fixtures. Run cheap unit tests on every change and reserve the full toolchain matrix for the appropriate CI jobs.

The tracked repository has no CI workflow. Add minimum/current Python and parser checks, a clean-install smoke test, and failure-path tests for M1–M8. Preserve upstream attribution while adding fork-specific release URLs and platform support. Make the shell bootstrap recover from an existing but incomplete virtual environment.

## Noted

These choices are deliberate or already sound within their limits.

- Generated commands use argument vectors. User-provided command strings intentionally run through a shell. No generated-command shell injection was demonstrated in this review.
- CLI source selection rejects files outside the chosen root. Namespace output paths reject empty, `.` and `..` components. These are useful checks to preserve.
- Timeouts kill the child process group, and worker teardown avoids following symlinks while deleting the overlay. Keep these protections while changing worker storage.
- Timeout and compilation failure count as killed under the README's current policy. This is a metric policy, not by itself a defect. A missing runner or broken unmutated worker is different, as M1 demonstrates.
- The score excludes uncovered sites. That is documented, but ownership documentation should make clear that a high mutation score can coexist with many uncovered sites.
- Crapper extraction gaps for Clojure reader conditionals and JavaScript private/variable-bound functions also remove mutation candidates. Fix the shared extractor and add integration assertions rather than maintaining a second function model in mutator.

## Dismissed

These are not useful default responses to the findings.

- More workers cannot repair false scores. Correct worker execution and cache validity come first.
- A passing test count does not justify removing the existing suite. Preserve its useful assertions and add the missing execution cases.
- Combining all three repositories or introducing a plugin framework is not necessary to fix these issues. A small versioned shared API is sufficient if separate releases remain useful.

## Proposed order

1. Fix M3–M4 before broad unattended use. Source and fixture preservation are the first acceptance criteria.
2. Fix M1–M2 and M5. Require a valid unmutated worker and correct source loading before interpreting any kill or survivor.
3. Fix M6–M8 with crapper changes, then add the real-language execution matrix and clean-install CI.
4. Reduce snapshot writes and repeated analysis under M9–M10. Compare the same project, cache state, worker count, runtime, memory use, and outcome set before and after.
5. Remove obsolete recovery helpers and complete the release/dependency documentation. Keep a full-run escape hatch while the revised incremental cache gains evidence.
