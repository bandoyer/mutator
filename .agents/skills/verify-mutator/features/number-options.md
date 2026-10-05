# Number options

The options that take a number (`--timeout-factor <number>`, `--baseline-timeout <seconds>`, and `--max-workers <number>`) accept only a finite number greater than 0. Any other value, such as `0`, `inf`, `nan`, or `1e309`, stops mutator before it runs a test: exit code `1`, a message that names the option, and the help. `--mutation-warning <count>` is a count of sites, and `0` is a valid count. Text that isn't a number, such as `abc`, gets the same kind of message for every number option and for `--lines <n,n,...>`. A huge but finite value is accepted: every test command's timeout is bounded at 2,000,000 s (about 23 days), so a mutant or baseline timeout past what `poll()` accepts (2^31 - 1 ms) doesn't crash the run.

## Sub-features

- `number-not-finite` rejects `inf`, `nan`, and `1e309` for `--timeout-factor` before any test command starts.
- `number-zero` rejects `0` for every number option.
- `number-not-a-number` rejects `abc` for every number option and `--lines`, with a message that names the option.
- `number-huge-timeout` runs to the end with a finite `--timeout-factor` or `--baseline-timeout` so large that the timeout would pass `poll()`'s limit.
- `number-help` lists each option with its placeholder in `--help`.

## How to get to it (user POV)

- Pass a number option to any mutator command.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `T` is the transcript path for the feature.
- `project=$($vm project fixture)`.

- **number-not-finite.** For each `v` in `inf`, `nan`, and `1e309`, run `timeout -k 2 30 $vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --timeout-factor $v --test-command 'sleep 3' demo.py`. Pass: exit code `1`, stderr starts with `--timeout-factor requires a finite positive number`, no `Traceback`, no `KILLED` line, and processes left `(none)`. The bug shows as an `OverflowError` traceback with a `sleep 3` left running (`inf`, `1e309`), or as exit `0` with the mutant counted as killed (`nan`).
- **number-zero.** For each option in `--timeout-factor`, `--baseline-timeout`, and `--max-workers`, and each `v` in `0`, `inf`, and `nan`, run `$vm drive "$project" "$T" <option> $v demo.py`. Pass: exit code `1` and a message that names the option. Then run `$vm drive "$project" "$T" --no-coverage --mutate-all --mutation-warning 0 demo.py`. Pass: it runs the mutants (exit `0`, or `3` when one survives) and stderr has `WARNING: <n> covered mutations selected in demo.py.`
- **number-not-a-number.** For each option in `--timeout-factor`, `--baseline-timeout`, `--mutation-warning`, `--max-workers`, and `--lines`, run `$vm drive "$project" "$T" <option> abc demo.py`. Pass: exit code `1`, stderr starts with `<option> requires`, and no `could not convert` or `invalid literal` text. The bug shows as Python's own message, such as `could not convert string to float: 'abc'`.
- **number-huge-timeout.** For each of `--timeout-factor 1e308`, `--timeout-factor 1e6`, and `--baseline-timeout 1e7`, run `timeout -k 2 60 $vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 <option> <value> --test-command 'sleep 3' demo.py`. Pass: the score table prints, exit code `3` (the `sleep 3` "test" always passes, so every mutant survives), no `Traceback`, and processes left `(none)`. The bug shows as an `OverflowError` traceback, exit `1`, with a `sleep 3` left running. `1e6` crashes with a baseline of 3 s because 3e6 s is past `poll()`'s limit of about 2,147,483 s.
- **number-help.** Run `$vm drive "$project" "$T" --help`. Pass: the help lists `--timeout-factor <number>`, `--baseline-timeout <seconds>`, `--mutation-warning <count>`, and `--max-workers <number>`.

## Gotchas

- `number-not-finite` and `number-huge-timeout` leave a `sleep 3` running when the bug is back. Run it inside one `bin/sandbox` call, so the process dies with the sandbox.
