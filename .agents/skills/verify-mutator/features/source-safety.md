# Source safety

mutator never writes to the project's real source. Mutants run in worker folders, so an edit the user saves to a source file while the tests run is still there afterwards. Older versions of mutator mutated the source in place and kept a copy under `target/mutator-backup/`. Before a non-scan run, mutator deletes a backup whose bytes equal its source. When a backup differs from its source, mutator stops before it runs a test: exit code `1`, a message that names both files and how to recover, the source unchanged, and the backup kept.

## Sub-features

- `source-edit-during-run` keeps an edit the user saves to the source while a mutant runs.
- `source-stale-backup` stops with exit code `1` when a backup differs from its source, and writes nothing.
- `source-equal-backup` deletes a backup equal to its source without a message, and the run goes on.

## How to get to it (user POV)

- Run any mutator command that runs mutants: edit a source file while it runs, or start it with a backup left under `target/mutator-backup/`.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `T` is the transcript path for the feature.
- `project=$($vm project fixture)`. The fixture ignores `target/`, so a backup there isn't a tracked file.

- **source-edit-during-run.** Run `$vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --test-command "case \$PWD in *mutation-workers*) grep -q 'user edit' $project/demo.py || printf '# user edit\n' >> $project/demo.py ;; esac; python3 -c 'import demo; assert demo.f() is True'" demo.py`. The test command saves the edit into the real `demo.py` on its first run in a worker. Then run `$vm exec "$project" "$T" git diff`. Pass: exit code `0`, three `KILLED` lines, tracked files changed lists `M demo.py`, and the diff adds `# user edit`. The bug shows as tracked files changed `(none)` and an empty diff.
- **source-stale-backup.** Run `$vm exec "$project" "$T" sh -c 'mkdir -p target/mutator-backup && printf "def f():\n    return 1 == 2\n" > target/mutator-backup/demo.py'`. Then run `$vm drive "$project" "$T" --no-coverage --mutate-all demo.py`, then `$vm exec "$project" "$T" cat target/mutator-backup/demo.py`. Pass: exit code `1`, stderr names `demo.py` and `target/mutator-backup/demo.py` and says how to recover, no `KILLED` line, tracked files changed `(none)`, and the backup still holds `1 == 2`. The bug shows as `Restored …/demo.py from an interrupted mutation.`, exit code `2` because the restored `1 == 2` fails the baseline, tracked files changed `M demo.py`, and no backup left.
- **source-equal-backup.** Run `$vm exec "$project" "$T" sh -c 'mkdir -p target/mutator-backup && cp demo.py target/mutator-backup/demo.py'`. Then run `$vm drive "$project" "$T" --no-coverage --mutate-all demo.py`, then `$vm exec "$project" "$T" ls -A target/mutator-backup`. Pass: exit code `0`, three `KILLED` lines, stderr says nothing about a backup, tracked files changed `(none)`, and the backup folder is empty. The bug shows as `Restored …/demo.py from an interrupted mutation.` on stderr.

## Gotchas

- Use `$project`'s absolute path in the edit command. The command runs in the worker, where `demo.py` is the worker's own copy.
