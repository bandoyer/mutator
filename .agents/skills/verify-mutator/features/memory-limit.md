# Memory limit

Each test command mutator runs, for the baseline, a worker's control run, or a mutant, gets a limit on each process's data memory (`RLIMIT_DATA`). `--memory-limit <MiB>` sets it (default 2048; `0` means no limit). A process that tries to take more fails to allocate, as Python's `MemoryError`, so its tests fail: a mutant that allocates without bound is `KILLED` within seconds instead of at its timeout, and a baseline that needs more fails with exit code 2. The coverage command gets no limit.

## Sub-features

- `memory-runaway`: a mutant whose tests allocate without bound is `KILLED` at the limit, long before its timeout.
- `memory-default`: with no option, a test process can't take more than 2048 MiB.
- `memory-option`: `--memory-limit` sets the limit, `0` removes it, `--help` lists it, and a value that isn't a whole number of 0 or more is a usage error.

## How to get to it (user POV)

- Run any mutator command that runs tests, with or without `--memory-limit`.

## Driving it with verify-mutator

Preconditions:

- `$vm doctor` prints `doctor: ok`.
- `T` is the transcript path for the feature.
- Run each recipe inside one `bin/sandbox` call. For `memory-runaway`, start that sandbox with `SKILLFLOW_SANDBOX_MEMORY=4G`: when the bug is back, the sandbox's scope is killed at 4 GiB, as the issue's measure was at 8G (#58), instead of taking more of the shared budget.

- **memory-runaway.** `project=$($vm project fixture)`. Every fixture mutant makes `f()` false. Run `$vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --timeout-factor 1000 --test-command "python3 -c \"exec('import demo\nrows = []\nwhile not demo.f(): rows.append([0] * 1000)')\"" demo.py`. Pass: exit code `0`, three `KILLED` lines, and the drive ends within about 10 s, though each mutant's timeout is over 30 s. The bug shows as a drive the sandbox's scope kills (out of memory) or one that takes about 30 s per mutant.
- **memory-default.** `project=$($vm project fixture)`. Run `$vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --test-command "python3 -c 'bytes(3 << 30)'" demo.py`. `bytes()` asks for 3 GiB of zeroed memory, which the kernel gives without touching it. Pass: exit code `2`, and stderr shows the baseline failed with `MemoryError`. The bug shows as exit code `3`: the baseline passes, and the mutants survive a test that checks nothing.
- **memory-option.** On the same project: `$vm drive "$project" "$T" --no-coverage --mutate-all --max-workers 1 --memory-limit 0 --test-command "python3 -c 'bytes(3 << 30)'" demo.py` exits `3` (no limit). `--memory-limit 64` with `--test-command "python3 -c 'bytes(128 << 20)'"` exits `2` with `MemoryError`. `--memory-limit 2G` and `--memory-limit -1` exit `1` with a message that starts `--memory-limit requires`. `--help` lists `--memory-limit <MiB>` with `Default: 2048`.

## Gotchas

- The limit is per process: a test command that starts many processes can take more in total. The sandbox's scope caps the total.
- On macOS, the kernel doesn't hold `mmap` to `RLIMIT_DATA`, so the limit may not stop a process there.
