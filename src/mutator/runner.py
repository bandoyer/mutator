"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import contextlib
import io
import math
import os
import shlex
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from mutator.process import Command, ManagedProcess, spawn_process

# A command is waited for in slices this long, so a stop is noticed quickly.
# A slice also stays far below OS wait limits, so a huge --timeout-factor or
# --baseline-timeout can't overflow it.
SLICE = 0.1
# The longest any command is waited for, about 23 days. A huge or infinite
# --timeout-factor or --baseline-timeout gets this bound instead.
LONGEST_TIMEOUT = 2_000_000.0
# How long a command has to end after a soft stop before a hard kill. On POSIX
# a mutator run nested in a test command uses it to stop its own commands and clean up.
GRACE = 1.0
# Each test process's memory limit, in MiB, unless --memory-limit says otherwise.
# A mutant that allocates without bound then fails within seconds, not at its timeout.
MEMORY_LIMIT = 2048


@dataclass(frozen=True)
class CommandResult:
    code: int
    timed_out: bool
    seconds: float
    output: str


# new_run_dir writes this file into every run folder. A worker's real path keeps
# the run folder as a parent even when target or target/mutation-workers is a
# symlink to storage elsewhere, where neither name is left in the path.
RUN_MARKER = ".mutator-run"


def _worker_home(cwd: Path) -> Path | None:
    """The worker folder when cwd is inside one, found by its run folder's name or marker."""

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        if not candidate.name.startswith("worker-"):
            continue
        if not parent.name.startswith("run-"):
            continue
        if parent.parent.name == "mutation-workers" or (parent / RUN_MARKER).is_file():
            return candidate
    return None


def _source_entries(cwd: Path, worker: Path) -> list[str]:
    found = []
    for directory in (cwd / "src", cwd, worker / "src", worker):
        text = str(directory)
        if text in found:
            continue
        if directory.is_dir():
            found.append(text)
    return found


def _prefer_worker_sources(cwd: Path, environment: dict) -> None:
    """An editable install would otherwise import the unmutated tree."""

    worker = _worker_home(cwd)
    if worker is None:
        return
    entries = _source_entries(cwd, worker)
    current = environment.get("PYTHONPATH")
    if current:
        entries.append(current)
    if not entries:
        return
    environment["PYTHONPATH"] = os.pathsep.join(entries)


# Git settings that name a repository outright, as a hook that runs mutator sets them.
_GIT_LOCATIONS = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR")


def _keep_git_in_worker(cwd: Path, environment: dict) -> None:
    """A worker has no .git, so git would walk up to the project's own repository (issue #8).

    The worker's run folder becomes a ceiling git doesn't search past, so a
    test's `git add` or `git status` can't change the project's index.
    """

    worker = _worker_home(cwd)
    if worker is None:
        return
    for name in _GIT_LOCATIONS:
        environment.pop(name, None)
    ceilings = [str(worker.parent)]
    current = environment.get("GIT_CEILING_DIRECTORIES")
    if current:
        ceilings.append(current)
    environment["GIT_CEILING_DIRECTORIES"] = os.pathsep.join(ceilings)


def _keep_bytecode_beside_sources(cwd: Path, environment: dict) -> None:
    """A worker's commands keep any bytecode beside the worker's sources (issue #82).

    With PYTHONPYCACHEPREFIX, Python would read and write a mutant's .pyc in
    a mirror folder, where mutator can't drop it before the next mutant runs.
    """

    if _worker_home(cwd) is not None:
        environment.pop("PYTHONPYCACHEPREFIX", None)


def _use_worker_temp(environment: dict, temp_dir: Path | None) -> None:
    """Give a worker's commands the worker's own temp folder (issue #54).

    Workers that shared one would share pytest's basetemp root, where each
    session prunes the others' folders.
    """

    if temp_dir is None:
        return
    # A test can remove its own temp folder. Made again in the run's private
    # folder, it keeps the worker's next commands off a shared one.
    with contextlib.suppress(OSError):
        temp_dir.mkdir()
    value = str(temp_dir)
    environment["TMPDIR"] = value
    environment["TEMP"] = value
    environment["TMP"] = value


class Stopped(Exception):
    """Mutator is stopping, so the command was stopped or never started."""


class _Command:
    """A running command drained on a background thread without reaping its leader early."""

    def __init__(self, process: ManagedProcess):
        self.process = process
        self._chunks: list[bytes] = []
        self._lock = threading.Lock()
        self._eof = threading.Event()
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()

    def _drain(self) -> None:
        stream = self.process.stdout
        if stream is None:
            self._eof.set()
            return
        try:
            fd = stream.fileno()
            while True:
                data = os.read(fd, 32768)
                if not data:
                    break
                with self._lock:
                    self._chunks.append(data)
        except OSError:
            pass
        finally:
            self._eof.set()

    def stop_soft(self) -> bool:
        """Ask the command's process tree to stop cleanly."""

        return self.process.stop_soft()

    def stop_hard(self) -> bool:
        """Forcefully kill the command's process tree."""

        return self.process.stop_hard()

    def wait(self, timeout: float | None) -> str:
        """Wait for output EOF and leader exit, leaving the leader unreaped on POSIX.

        Return the output. Raise TimeoutExpired after `timeout` seconds; None waits as long as it takes.
        """

        deadline = math.inf if timeout is None else time.monotonic() + timeout
        delay = 0.0005  # Popen.wait's back-off: 1 ms, doubling to 50 ms
        while not self._eof.is_set() or not self._exited():
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            if not self._eof.is_set():
                self._read(min(left, SLICE))
            else:
                delay = min(delay * 2, left, 0.05)
                time.sleep(delay)
        return self._output()

    def _read(self, seconds: float) -> None:
        self._eof.wait(seconds)

    def _exited(self) -> bool:
        return self.process.has_exited()

    def _output(self) -> str:
        """The output read so far, decoded as Popen's text mode would."""

        with self._lock:
            raw = io.BytesIO(b"".join(self._chunks))
        # A byte the locale can't decode shows as U+FFFD instead of crashing the run.
        return io.TextIOWrapper(raw, encoding=None, errors="replace").read()

    def end(self, patience: float = 5.0) -> int:
        """Kill what is left in the process tree, reap the leader, and close the pipe."""

        code = self.process.reap(patience)
        self._reader.join(timeout=patience)
        if self.process.stdout is not None:
            self.process.stdout.close()
        return code


def _stop(command: _Command) -> str:
    """Stop the command's process tree, giving it GRACE before a hard kill. Return its output."""

    command.stop_soft()
    try:
        return command.wait(GRACE)
    except subprocess.TimeoutExpired:
        command.stop_hard()
        return command.wait(None)
    except BaseException:
        # Ctrl-C or SIGTERM during the grace: don't wait it out.
        command.stop_hard()
        raise


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False, memory_limit: int = 0, env: dict[str, str] | None = None):
        self.verbose = verbose
        self.memory_limit = memory_limit
        self.env = dict(env) if env else {}
        self._stopping = threading.Event()

    def stop(self) -> None:
        """Stop every command this runner is running, and start no more.

        Each command's own thread sees the stop within SLICE and stops it, so
        no thread signals a command that another thread may already have reaped.
        """

        self._stopping.set()

    def _communicate(self, command: _Command, timeout: float | None) -> str:
        """The command's output, waited for in slices. A timeout raises TimeoutExpired."""

        deadline = math.inf if timeout is None else time.monotonic() + min(timeout, LONGEST_TIMEOUT)
        while True:
            wait = max(0.0, min(SLICE, deadline - time.monotonic()))
            try:
                return command.wait(wait)
            except subprocess.TimeoutExpired:
                if self._stopping.is_set():
                    raise Stopped from None
                if time.monotonic() >= deadline:
                    raise

    def run(
        self,
        command: Command,
        cwd: Path,
        timeout: float | None,
        *,
        temp_dir: Path | None = None,
    ) -> CommandResult:
        """Run `command` in `cwd`.

        A list is an argument vector and does not go through a shell, so a
        path with spaces or metacharacters stays one argument. A string is a
        command the user typed in `--test-command` or `--coverage-command`.
        """

        if self._stopping.is_set():
            raise Stopped
        if self.verbose:
            print(f"+ ({cwd}) {display_command(command)}", file=sys.stderr)
        started = time.monotonic()
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        if self.env:
            environment.update(self.env)
        _prefer_worker_sources(cwd, environment)
        _keep_git_in_worker(cwd, environment)
        _use_worker_temp(environment, temp_dir)
        _keep_bytecode_beside_sources(cwd, environment)
        try:
            managed = spawn_process(command, cwd, environment, self.memory_limit)
        except OSError as exc:
            seconds = time.monotonic() - started
            result = CommandResult(code=127, timed_out=False, seconds=seconds, output=str(exc))
            return self._ended(cwd, result)
        running = _Command(managed)
        try:
            output, timed_out = self._finish(running, timeout)
        finally:
            code = running.end()
        code = 124 if timed_out else code
        seconds = time.monotonic() - started
        result = CommandResult(code=code, timed_out=timed_out, seconds=seconds, output=output)
        return self._ended(cwd, result)

    def _finish(self, command: _Command, timeout: float | None) -> tuple[str, bool]:
        """The command's output, and whether it timed out. It has ended when this returns."""

        try:
            return self._communicate(command, timeout), False
        except subprocess.TimeoutExpired:
            return _stop(command), True
        except BaseException:
            # Ctrl-C, SIGTERM, or a stop: the command must not outlive the run.
            _stop(command)
            raise

    def _ended(self, cwd: Path, result: CommandResult) -> CommandResult:
        # Workers run in parallel, so the folder ties this line to its command.
        if self.verbose:
            ending = ", timed out" if result.timed_out else ""
            print(f"= ({cwd}) exit {result.code} in {result.seconds:.1f} s{ending}", file=sys.stderr)
        return result


def nearest(start: Path, marker: str, stop: Path) -> Path | None:
    current = start if start.is_dir() else start.parent
    stop = stop.resolve()
    current = current.resolve()
    while True:
        if (current / marker).is_file():
            return current
        if current == stop or current.parent == current:
            return None
        current = current.parent


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _clojure_command(directory: Path) -> list[str]:
    deps = directory / "deps.edn"
    bb = directory / "bb.edn"
    if bb.is_file() and not deps.is_file():
        text = _read(bb)
        if "spec" in text:
            return ["bb", "spec", "--tag", "~no-mutate"]
        return ["bb", "test"]
    text = _read(deps)
    if ":spec" in text and "speclj" in text:
        return ["clj", "-M:spec", "--tag", "~no-mutate"]
    if ":spec" in text:
        return ["clj", "-M:spec"]
    return ["clj", "-M:test"]


def _project_interpreter(directory: Path) -> str:
    """The project's virtualenv, or this process when the project has none.

    The path stays absolute and the symlink is left in place. A worker does
    not link ``.venv``, and resolving ``bin/python`` would leave the virtualenv.
    """

    sub, exe = ("Scripts", "python.exe") if os.name == "nt" else ("bin", "python")
    for name in (".venv", "venv"):
        candidate = (directory / name / sub / exe).absolute()
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return sys.executable


def _python_command(directory: Path) -> list[str]:
    interpreter = _project_interpreter(directory)
    pyproject = _read(directory / "pyproject.toml")
    if (
        (directory / "pytest.ini").is_file()
        or (directory / "conftest.py").is_file()
        or "pytest" in pyproject
        or (directory / "setup.cfg").is_file() and "pytest" in _read(directory / "setup.cfg")
    ):
        return [interpreter, "-m", "pytest"]
    return [interpreter, "-m", "unittest", "discover"]


def _nearest_marker(source: Path, root: Path, markers: tuple[str, ...]) -> Path:
    for marker in markers:
        found = nearest(source, marker, root)
        if found is not None:
            return found
    return root


def _go_package(directory: Path, source: Path) -> str:
    relative = source.resolve().parent.relative_to(directory.resolve())
    if relative == Path("."):
        return "."
    return "./" + relative.as_posix()


def _clojure_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("deps.edn", "bb.edn"))
    return _clojure_command(directory), directory


def _java_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pom.xml",))
    return ["mvn", "-q", "test", "-DexcludeTags=no-mutate"], directory


def _go_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("go.mod",))
    return ["go", "test", "-count=1", _go_package(directory, source)], directory


def _typescript_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("package.json",))
    return ["npm", "test"], directory


def _rust_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("Cargo.toml",))
    return ["cargo", "test"], directory


def _python_plan(root: Path, source: Path) -> tuple[Command, Path]:
    directory = _nearest_marker(source, root, ("pyproject.toml", "pytest.ini", "setup.cfg"))
    return _python_command(directory), directory


_PLANS = {
    "clojure": _clojure_plan,
    "java": _java_plan,
    "go": _go_plan,
    "typescript": _typescript_plan,
    "rust": _rust_plan,
    "python": _python_plan,
}


def test_plan(root: Path, source: Path, language: str, override: Command | None) -> tuple[Command, Path]:
    """The test command and the directory it runs in.

    A string override stays a shell string. A list override or generated
    command is an argument vector, so paths and flags stay verbatim arguments.
    """

    root = root.resolve()
    if override:
        return (list(override) if isinstance(override, list) else override), root
    plan = _PLANS.get(language)
    if plan is None:
        return [sys.executable, "-c", "raise SystemExit(1)"], root
    return plan(root, source)
