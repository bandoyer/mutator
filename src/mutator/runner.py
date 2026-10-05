"""Run a project's tests, and decide the command for each language."""

from __future__ import annotations

import math
import os
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
# How long a command has to end after SIGTERM before it gets SIGKILL. A mutator
# run nested in a test command uses it to stop its own commands and clean up.
GRACE = 1.0


@dataclass(frozen=True)
class CommandResult:
    code: int
    timed_out: bool
    seconds: float
    output: str


def _worker_home(cwd: Path) -> Path | None:
    """The overlay root when cwd is inside target/mutation-workers."""

    for candidate in [cwd, *cwd.parents]:
        parent = candidate.parent
        if not candidate.name.startswith("worker-"):
            continue
        if not parent.name.startswith("run-"):
            continue
        if parent.parent.name == "mutation-workers":
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


class Stopped(Exception):
    """Mutator is stopping, so the command was stopped or never started."""


def _signal_group(process: subprocess.Popen, sig: int) -> None:
    """Signal the command's group while its leader is unreaped.

    Until the leader is reaped, even as a zombie, its pid stays the id of its
    group, so the signal reaches only the command's processes. Popen sets
    returncode when it reaps. An exception that lands inside that reap leaves
    returncode unset, so the kernel is asked too: WNOWAIT reaps nothing, and a
    leader already reaped raises ChildProcessError. Only the thread that runs
    the command calls this, so nothing reaps it between the check and the signal.
    """

    if process.returncode is not None:
        return
    if hasattr(os, "waitid"):  # macOS has it from Python 3.13
        try:
            os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
        except ChildProcessError:
            return
    _kill_group(process.pid, sig)


def _stop(process: subprocess.Popen) -> str:
    """SIGTERM the command's group, give it GRACE to end, then SIGKILL it. Return its output."""

    _signal_group(process, signal.SIGTERM)
    try:
        output, _err = process.communicate(timeout=GRACE)
    except subprocess.TimeoutExpired:
        _signal_group(process, signal.SIGKILL)
        output, _err = process.communicate()
    return output or ""


def _kill_group(group: int, sig: int = signal.SIGKILL) -> None:
    """Send a signal to the process group a command leads.

    Linux implements killpg(group) as kill(-group), so group 1 means every
    process this user may signal and group 0 means mutator's own group. A
    command started in a new session leads a group above 1.
    """

    if group <= 1:
        raise ValueError(f"refusing to signal process group {group}")
    os.killpg(group, sig)


def _end_group(group: int, patience: float = 5.0) -> None:
    """Kill what the command left in its process group, and wait until it is gone.

    A worker is removed as soon as its command returns. A process still running
    there can add a file while the folder is being removed. A background child
    does that, and so does a rustc that has been sent SIGKILL but not yet exited.
    """

    deadline = time.monotonic() + patience
    while time.monotonic() < deadline:
        try:
            _kill_group(group)
        except ProcessLookupError:
            return
        time.sleep(0.01)


def display_command(command: Command) -> str:
    """A shell-looking rendering. A list is quoted so it can be pasted."""

    if isinstance(command, str):
        return command
    return " ".join(shlex.quote(part) for part in command)


class CommandRunner:
    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self._stopping = threading.Event()

    def stop(self) -> None:
        """Stop every command this runner is running, and start no more.

        Each command's own thread sees the stop within SLICE and stops it, so
        no thread signals a command that another thread may already have reaped.
        """

        self._stopping.set()

    def _communicate(self, process: subprocess.Popen, timeout: float | None) -> str:
        """The command's output, waited for in slices. A timeout raises TimeoutExpired."""

        deadline = math.inf if timeout is None else time.monotonic() + timeout
        while True:
            wait = max(0.0, min(SLICE, deadline - time.monotonic()))
            try:
                output, _err = process.communicate(timeout=wait)
                return output or ""
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
        try:
            process = subprocess.Popen(
                command,
                cwd=cwd,
                shell=isinstance(command, str),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                text=True,
                env=environment,
            )
        except OSError as exc:
            seconds = time.monotonic() - started
            result = CommandResult(code=127, timed_out=False, seconds=seconds, output=str(exc))
            return self._ended(cwd, result)
        try:
            output = self._communicate(process, timeout)
            code = process.returncode if process.returncode is not None else 1
            timed_out = False
        except subprocess.TimeoutExpired:
            output = _stop(process)
            code = 124
            timed_out = True
        except BaseException:
            # Ctrl-C, SIGTERM, or a stop: the command must not outlive the run.
            _stop(process)
            raise
        _end_group(process.pid)
        seconds = time.monotonic() - started
        result = CommandResult(code=code, timed_out=timed_out, seconds=seconds, output=output)
        return self._ended(cwd, result)

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
