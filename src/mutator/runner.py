"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import io
import math
import os
import selectors
import shlex
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

Command = str | list[str]

# A command is waited for in slices this long, so a stop is noticed quickly.
# A slice also stays far below poll()'s limit of 2**31 - 1 ms, so a huge
# --timeout-factor or --baseline-timeout can't overflow it.
SLICE = 0.1
# The longest any command is waited for, about 23 days. A huge or infinite
# --timeout-factor or --baseline-timeout gets this bound instead.
LONGEST_TIMEOUT = 2_000_000.0
# How long a command has to end after SIGTERM before it gets SIGKILL. A mutator
# run nested in a test command uses it to stop its own commands and clean up.
GRACE = 1.0
# Each test process's data memory, in MiB, unless --memory-limit says otherwise.
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


class Stopped(Exception):
    """Mutator is stopping, so the command was stopped or never started."""


class _Command:
    """A running command, read and waited for without reaping its leader.

    Until the leader is reaped, even as a zombie, its pid stays the id of its
    group, so a signal to the group reaches only the command's processes.
    Popen.communicate() reaps the leader as soon as it exits, while other
    processes of its group may still run. So the output is read here, and
    end() reaps the leader only after it has killed the rest of the group.
    """

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self._chunks: list[bytes] = []
        self._open = True
        # Set before every call that can reap, so an exception inside one can't hide a reap.
        self._may_be_reaped = False
        self._selector = selectors.DefaultSelector()
        self._selector.register(process.stdout, selectors.EVENT_READ)

    def signal(self, sig: int) -> bool:
        """Signal the command's group while its leader is surely unreaped. Return whether it did."""

        if self._may_be_reaped:
            return False
        _kill_group(self.process.pid, sig)
        return True

    def wait(self, timeout: float | None) -> str:
        """Read the output to its end and wait until the leader exits, leaving it unreaped.

        Return the output. Raise TimeoutExpired after `timeout` seconds; None waits as long as it takes.
        """

        deadline = math.inf if timeout is None else time.monotonic() + timeout
        delay = 0.0005  # Popen.wait's back-off: 1 ms, doubling to 50 ms
        while self._open or not self._exited():
            left = deadline - time.monotonic()
            if left <= 0:
                raise subprocess.TimeoutExpired(self.process.args, timeout)
            if self._open:
                self._read(min(left, SLICE))
            else:
                delay = min(delay * 2, left, 0.05)
                time.sleep(delay)
        return self._output()

    def _read(self, seconds: float) -> None:
        if self._selector.select(seconds):
            data = os.read(self.process.stdout.fileno(), 32768)
            self._chunks.append(data)
            self._open = bool(data)

    def _exited(self) -> bool:
        """Whether the leader has exited. Only where os.waitid is missing does this reap it.

        It is called only once the output has ended, so a leader it reaps
        leaves nothing that holds the output for wait() to wait on.
        """

        if hasattr(os, "waitid"):  # macOS has it from Python 3.13
            return os.waitid(os.P_PID, self.process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
        self._may_be_reaped = True
        self._may_be_reaped = self.process.poll() is not None
        return self._may_be_reaped

    def _output(self) -> str:
        """The output read so far, decoded as Popen's text mode would."""

        raw = io.BytesIO(b"".join(self._chunks))
        stream = self.process.stdout
        return io.TextIOWrapper(raw, encoding=stream.encoding, errors=stream.errors).read()

    def end(self, patience: float = 5.0) -> int:
        """Kill what is left in the group, reap the leader, and wait until the group is gone.

        Return the leader's exit code. One SIGKILL is enough: on Linux, a fork
        that races a signal to its group either passes the signal to the child
        or fails. A process still running there can add a file while the worker
        folder is being removed, as a background child or a dying rustc does.
        """

        signalled = self.signal(signal.SIGKILL)
        self._may_be_reaped = True
        code = self.process.wait()
        self._selector.close()
        self.process.stdout.close()
        if signalled:
            _wait_until_gone(self.process.pid, patience)
        return code


def _stop(command: _Command) -> str:
    """SIGTERM the command's group, give it GRACE to end, then SIGKILL it. Return its output."""

    command.signal(signal.SIGTERM)
    try:
        return command.wait(GRACE)
    except subprocess.TimeoutExpired:
        command.signal(signal.SIGKILL)
        return command.wait(None)
    except BaseException:
        # Ctrl-C or SIGTERM during the grace: don't wait it out.
        command.signal(signal.SIGKILL)
        raise


def _kill_group(group: int, sig: int = signal.SIGKILL) -> None:
    """Send a signal to the process group a command leads.

    Linux implements killpg(group) as kill(-group), so group 1 means every
    process this user may signal and group 0 means mutator's own group. A
    command started in a new session leads a group above 1.
    """

    if group <= 1:
        raise ValueError(f"refusing to signal process group {group}")
    os.killpg(group, sig)


def _wait_until_gone(group: int, patience: float) -> None:
    """Wait until no process is left in the group, for at most `patience` seconds.

    The leader is reaped by now, so its id is no longer known to be the
    command's. Signal 0 sends nothing; it only asks whether the group exists.
    """

    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        try:
            _kill_group(group, 0)
        except ProcessLookupError:
            return
        time.sleep(0.01)


def _limited(command: Command, megabytes: int) -> Command:
    """The command, with each process's data memory (RLIMIT_DATA) limited to `megabytes` MiB. 0 is no limit.

    A shell sets the limit and then execs the command, so the limit is set in
    the child, and every process the command starts inherits it. It sets the
    hard limit too, so no process can raise it again. A hard limit that is
    already lower stays, and the command still runs. ulimit -d takes KiB, and
    rlim_t holds bytes only below 2**64, so a larger limit is none.
    """

    if not megabytes:
        return command
    argv = ["/bin/sh", "-c", command] if isinstance(command, str) else command
    kib = str(megabytes * 1024) if megabytes < 2**44 else "unlimited"
    return ["/bin/sh", "-c", 'ulimit -d "$1" 2>/dev/null; shift; exec "$@"', "sh", kib, *argv]


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False, memory_limit: int = 0):
        self.verbose = verbose
        self.memory_limit = memory_limit
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

    def run(self, command: Command, cwd: Path, timeout: float | None) -> CommandResult:
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
        _prefer_worker_sources(cwd, environment)
        _keep_git_in_worker(cwd, environment)
        limited = _limited(command, self.memory_limit)
        try:
            process = subprocess.Popen(
                limited,
                cwd=cwd,
                shell=isinstance(limited, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                # A byte the locale can't decode shows as U+FFFD instead of crashing the run.
                errors="replace",
                env=environment,
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            result = CommandResult(code=127, timed_out=False, seconds=seconds, output=str(exc))
            return self._ended(cwd, result)
        command = _Command(process)
        try:
            output, timed_out = self._finish(command, timeout)
        finally:
            code = command.end()
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

    for name in (".venv", "venv"):
        candidate = (directory / name / "bin" / "python").absolute()
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


def test_plan(root: Path, source: Path, language: str, override: str | None) -> tuple[Command, Path]:
    """The test command and the directory it runs in.

    A user override stays a shell string. Every generated command is an
    argument vector, so a package path is one argument.
    """

    root = root.resolve()
    if override:
        return override, root
    plan = _PLANS.get(language)
    if plan is None:
        return ["false"], root
    return plan(root, source)
