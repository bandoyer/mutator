# Python with mutmut

Use this recipe for `--python-backend mutmut` or a change to the pinned
mutmut version. The native recipes continue to use the default backend.

Preconditions: `$vm doctor` passes, and mutator's `.venv` has the `python`
extra installed. Run the whole drive inside the project's process sandbox.
`T` is a transcript outside the scratch project.

1. Make `project=$($vm project fixture)`. Run
   `$vm drive "$project" "$T" --python-backend mutmut --scan demo.py`.
   Expect exit 0, mutation sites, no snapshot, and unchanged tracked files.
2. Run `$vm drive "$project" "$T" --python-backend mutmut --max-workers 2 demo.py`.
   Expect three killed mutants and exit 0. Check
   `.metrics/mutate/demo.edn` for the `defn/f` form and mutation IDs ending
   in `mutmut==3.8.0`. Worker folders and running worker processes are absent.
3. In the scratch project, save the snapshot hash and change its test to
   `def test_f(): assert False`. Commit that deliberate fixture edit so the
   drive can distinguish it from tool side effects. Drive the same command.
   Expect exit 2, unchanged snapshot hash, no worker folders or processes,
   and no tracked changes from the tool.
4. Clean up the scratch project with `$vm cleanup "$project"`.

For a performance trial, use a fixed cloned project and the same selected
source lines, interpreter, test suite, and worker cap for both engines.
Report mutant counts alongside duration because the operator sets differ.
Run the unmodified suite first. A failed clean run is an incompatibility,
not a measurement of mutation speed. The initial skillflow experiment is
in `benchmarks/python-mutmut.md` at the repository root.
