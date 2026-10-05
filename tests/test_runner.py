import ast
import math
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import wait_until

from mutator.runner import (
    CommandRunner,
    Stopped,
    _clojure_command,
    _end_group,
    _kill_group,
    _python_command,
    _signal_group,
    nearest,
)
from mutator.runner import test_plan as plan_command


def test_nearest_walks_up_to_the_marker(tmp_path):
    root = tmp_path / "proj"
    nested = root / "src" / "demo"
    nested.mkdir(parents=True)
    (root / "pyproject.toml").write_text("", encoding="utf-8")
    source = nested / "app.py"
    source.write_text("x = 1\n", encoding="utf-8")
    assert nearest(source, "pyproject.toml", root) == root
    assert nearest(nested, "pyproject.toml", root) == root
    assert nearest(source, "missing.toml", root) is None


def test_clojure_commands(tmp_path):
    bb = tmp_path / "bb-spec"
    bb.mkdir()
    (bb / "bb.edn").write_text('{:tasks {spec "spec"}}', encoding="utf-8")
    assert _clojure_command(bb) == ["bb", "spec", "--tag", "~no-mutate"]

    bb_test = tmp_path / "bb-test"
    bb_test.mkdir()
    (bb_test / "bb.edn").write_text("{:tasks {test (clojure \"-M:test\")}}", encoding="utf-8")
    assert _clojure_command(bb_test) == ["bb", "test"]

    spec = tmp_path / "spec"
    spec.mkdir()
    (spec / "deps.edn").write_text(
        "{:aliases {:spec {:extra-deps {speclj/speclj {}}}}}", encoding="utf-8"
    )
    assert _clojure_command(spec) == ["clj", "-M:spec", "--tag", "~no-mutate"]

    spec_only = tmp_path / "spec-only"
    spec_only.mkdir()
    (spec_only / "deps.edn").write_text("{:aliases {:spec {}}}", encoding="utf-8")
    assert _clojure_command(spec_only) == ["clj", "-M:spec"]

    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "deps.edn").write_text("{:deps {}}", encoding="utf-8")
    assert _clojure_command(plain) == ["clj", "-M:test"]


def _executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)


def test_python_command_uses_an_absolute_project_interpreter(tmp_path):
    project = tmp_path / "proj"
    dot = project / ".venv" / "bin" / "python"
    _executable(dot)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(project) == [str(dot.absolute()), "-m", "pytest"]

    other = tmp_path / "other"
    plain = other / "venv" / "bin" / "python"
    _executable(plain)
    assert _python_command(other) == [str(plain.absolute()), "-m", "unittest", "discover"]

    _executable(other / ".venv" / "bin" / "python")
    assert _python_command(other) == [
        str((other / ".venv" / "bin" / "python").absolute()),
        "-m",
        "unittest",
        "discover",
    ]


def test_python_command_keeps_the_virtualenv_symlink(tmp_path):
    project = tmp_path / "proj"
    target = project / "real-python"
    _executable(target)
    link = project / ".venv" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    (project / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(project) == [str(link.absolute()), "-m", "pytest"]


def test_python_command_falls_back_when_the_virtualenv_cannot_run(tmp_path):
    project = tmp_path / "proj"
    binary = project / ".venv" / "bin" / "python"
    binary.parent.mkdir(parents=True)
    binary.write_text("", encoding="utf-8")
    assert _python_command(project) == [sys.executable, "-m", "unittest", "discover"]

    missing = tmp_path / "missing"
    missing.mkdir()
    assert _python_command(missing) == [sys.executable, "-m", "unittest", "discover"]


def test_python_commands(tmp_path):
    pytest_dir = tmp_path / "py"
    pytest_dir.mkdir()
    (pytest_dir / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    assert _python_command(pytest_dir)[-2:] == ["-m", "pytest"]

    unit = tmp_path / "unit"
    unit.mkdir()
    assert _python_command(unit)[-3:] == ["-m", "unittest", "discover"]

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "setup.cfg").write_text("[tool:pytest]\n", encoding="utf-8")
    assert _python_command(cfg)[-2:] == ["-m", "pytest"]

    project = tmp_path / "proj"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\ndependencies=['pytest']\n", encoding="utf-8")
    assert _python_command(project)[-2:] == ["-m", "pytest"]

    conf = tmp_path / "conf"
    conf.mkdir()
    (conf / "conftest.py").write_text("", encoding="utf-8")
    assert _python_command(conf)[-2:] == ["-m", "pytest"]


def test_a_go_package_path_is_one_argument(tmp_path):
    root = tmp_path / "proj"
    package = root / "a;echo no"
    package.mkdir(parents=True)
    (root / "go.mod").write_text("module example.com/demo\n", encoding="utf-8")
    source = package / "widget.go"
    source.write_text("package main\nfunc Run() int { return 1 }\n", encoding="utf-8")
    command, directory = plan_command(root, source, "go", None)
    assert command == ["go", "test", "-count=1", "./a;echo no"]
    assert directory == root
    echoed = CommandRunner().run(["/bin/echo", command[-1]], directory, None)
    assert echoed.code == 0
    assert echoed.output.strip() == "./a;echo no"

    spaced = root / "my dir" / "widget.go"
    spaced.parent.mkdir()
    spaced.write_text("package main\nfunc Run() int { return 1 }\n", encoding="utf-8")
    command, _directory = plan_command(root, spaced, "go", None)
    assert command == ["go", "test", "-count=1", "./my dir"]
    override, override_dir = plan_command(root, source, "go", "go test ./...")
    assert override == "go test ./..."
    assert override_dir == root.resolve()


def test_a_worker_overlay_is_imported_ahead_of_the_environment(tmp_path):
    worker = tmp_path / "target" / "mutation-workers" / "run-1" / "worker-0"
    (worker / "src").mkdir(parents=True)
    (worker / "src" / "demo.py").write_text("VALUE = 'worker'\n", encoding="utf-8")
    command = f"{sys.executable} -c 'import demo; print(demo.VALUE)'"
    result = CommandRunner().run(command, worker, 5)
    assert result.code == 0
    assert result.output.strip() == "worker"


def test_only_a_group_the_command_leads_is_signalled(monkeypatch):
    sent = []
    monkeypatch.setattr("mutator.runner.os.killpg", lambda group, sig: sent.append((group, sig)))
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for group in (1, 0, -7):
            with pytest.raises(ValueError):
                _kill_group(group, sig)
    assert sent == []
    _kill_group(4242, signal.SIGTERM)
    _kill_group(4242, signal.SIGKILL)
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]


def test_nothing_the_command_started_outlives_the_run(tmp_path):
    late = tmp_path / "late.txt"
    command = f"(sleep 0.3; echo late > {shlex.quote(str(late))}) >/dev/null 2>&1 &"
    result = CommandRunner().run(command, tmp_path, 5)
    assert result.code == 0
    time.sleep(0.6)
    assert not late.exists()


def test_a_timeout_longer_than_poll_accepts_still_runs_the_command(tmp_path):
    # poll() takes at most 2**31 - 1 ms, about 24.8 days. --baseline-timeout 1e7
    # passes 1e7 s, and a 3 s baseline times --timeout-factor 1e308 is inf.
    for timeout in (1e7, math.inf):
        result = CommandRunner().run("sleep 0.2", tmp_path, timeout)
        assert (result.code, result.timed_out) == (0, False), timeout


def test_the_run_waits_until_the_group_is_gone(monkeypatch):
    signals = []

    def kill_group(group):
        signals.append(group)
        if len(signals) == 3:
            raise ProcessLookupError

    monkeypatch.setattr("mutator.runner._kill_group", kill_group)
    monkeypatch.setattr("mutator.runner.time.sleep", lambda seconds: None)
    _end_group(4242)
    assert signals == [4242, 4242, 4242]


def test_the_wait_for_the_group_gives_up_at_the_deadline(monkeypatch):
    signals = []
    clock = iter([0.0, 1.0, 4.0, 5.0])
    monkeypatch.setattr("mutator.runner._kill_group", signals.append)
    monkeypatch.setattr("mutator.runner.time.monotonic", lambda: next(clock))
    monkeypatch.setattr("mutator.runner.time.sleep", lambda seconds: None)
    _end_group(4242, patience=5.0)
    assert signals == [4242, 4242]


OTHER_SENDERS = ("send_signal", "terminate", "pthread_kill", "raise_signal", "pidfd_send_signal")


def _signal_senders():
    """Each place in src/ that names a way to signal a process, as (file, function)."""

    found = []
    for path in sorted((Path(__file__).resolve().parents[1] / "src").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        owners = {}
        for function in ast.walk(tree):
            if isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for node in ast.walk(function):
                    owners.setdefault(node, function.name)
        for node in ast.walk(tree):
            imported = isinstance(node, ast.ImportFrom) and node.module == "os"
            if imported and any(alias.name in ("kill", "killpg") for alias in node.names):
                found.append((path.name, owners.get(node, "<module>")))
            named = isinstance(node, ast.Attribute) and node.attr in ("kill", "killpg")
            if named and isinstance(node.value, ast.Name) and node.value.id == "os":
                found.append((path.name, owners.get(node, "<module>")))
            # Popen's and signal's own senders reach a pid without the guard too.
            if isinstance(node, ast.Attribute) and node.attr in OTHER_SENDERS:
                found.append((path.name, owners.get(node, "<module>")))
    return found


def test_signals_go_only_through_the_group_guard():
    # killpg(1) is kill(-1) on Linux: it reaches every process the user owns.
    assert _signal_senders() == [("runner.py", "_kill_group")]


def test_verbose_reports_how_each_command_ended(tmp_path, capsys):
    runner = CommandRunner(verbose=True)
    runner.run("exit 3", tmp_path, None)
    runner.run(f"{sys.executable} -c 'import time; time.sleep(30)'", tmp_path, 0.4)
    runner.run(["/no/such/program"], tmp_path, None)

    lines = capsys.readouterr().err.splitlines()
    assert lines[1].startswith(f"= ({tmp_path}) exit 3 in ")
    assert lines[3].startswith(f"= ({tmp_path}) exit 124 in ")
    assert lines[3].endswith(" s, timed out")
    assert lines[5].startswith(f"= ({tmp_path}) exit 127 in ")


def test_ctrl_c_stops_the_command_it_interrupts(tmp_path, ctrl_c_when):
    started = tmp_path / "started"
    late = tmp_path / "late"
    ctrl_c_when(started)

    with pytest.raises(KeyboardInterrupt):
        CommandRunner().run(f"touch {started}; sleep 2; touch {late}", tmp_path, None)

    wait_until(started, 2.5)
    assert not late.exists()


def test_ctrl_c_does_not_wait_for_what_a_finished_command_left_holding_its_output(tmp_path, ctrl_c_when):
    # The shell exits at once; its background sleep keeps the output pipe open.
    # Python's own Ctrl-C handling then reaps the shell, so its group is no longer
    # known to be the command's, and the run must not wait for the sleep.
    started = tmp_path / "started"
    ctrl_c_when(started)

    with pytest.raises(KeyboardInterrupt):
        CommandRunner().run(f"touch {started}; sleep 3 & exit 0", tmp_path, None)

    assert time.time() - started.stat().st_mtime < 2.0


def test_ctrl_c_during_the_grace_kills_the_command_at_once(tmp_path, ctrl_c_when):
    graced = tmp_path / "graced"
    late = tmp_path / "late"
    ignores_sigterm = f"trap '' TERM; (sleep 0.6; touch {graced}) & sleep 2; touch {late}"
    ctrl_c_when(graced)

    with pytest.raises(KeyboardInterrupt):
        CommandRunner().run(ignores_sigterm, tmp_path, 0.3)

    wait_until(graced, 1.8)
    assert not late.exists()


def test_a_timed_out_command_gets_sigterm_before_sigkill(tmp_path):
    termed = tmp_path / "termed"
    command = f"trap 'touch {termed}; exit 0' TERM; sleep 30 & wait"
    result = CommandRunner().run(command, tmp_path, 0.3)

    assert result.timed_out
    assert termed.exists()


def test_a_timed_out_command_that_ignores_sigterm_is_killed_after_the_grace(tmp_path):
    started = time.monotonic()
    result = CommandRunner().run("echo before; trap '' TERM; sleep 30", tmp_path, 0.3)

    assert result.timed_out
    assert result.output == "before\n"
    assert time.monotonic() - started < 0.3 + 1.0 + 1.0


class SlowCommand:
    """A command that never ends, on a clock that moves only while it is waited for."""

    def __init__(self):
        self.now = 0.0
        self.waits = []

    def communicate(self, timeout):
        self.waits.append(timeout)
        assert len(self.waits) < 10, self.waits
        self.now += timeout
        raise subprocess.TimeoutExpired("slow", timeout)


def test_the_wait_comes_in_slices_and_the_last_one_ends_at_the_timeout(monkeypatch):
    command = SlowCommand()
    monkeypatch.setattr("mutator.runner.SLICE", 0.25)
    monkeypatch.setattr("mutator.runner.time.monotonic", lambda: command.now)

    with pytest.raises(subprocess.TimeoutExpired):
        CommandRunner()._communicate(command, 0.625)

    assert command.waits == [0.25, 0.25, 0.125]


def test_an_endless_timeout_is_bounded(monkeypatch):
    command = SlowCommand()
    monkeypatch.setattr("mutator.runner.SLICE", 1_000_000.0)
    monkeypatch.setattr("mutator.runner.time.monotonic", lambda: command.now)

    with pytest.raises(subprocess.TimeoutExpired):
        CommandRunner()._communicate(command, math.inf)

    assert command.waits == [1_000_000.0, 1_000_000.0]


NESTED_RUN = "import sys\nfrom mutator.cli import run\nsys.exit(run(sys.argv[1:]))\n"


def test_a_timed_out_command_lets_a_nested_run_clean_up(tmp_path):
    # Issue #21: mutator's own tests call run() in the test process, so a
    # mutant can start a whole mutation run inside a worker's test command.
    project = tmp_path / "project"
    project.mkdir()
    (project / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    started = tmp_path / "started"
    late = tmp_path / "late"
    inner_tests = f'case "$PWD" in *mutation-workers*) touch {started}; sleep 5; touch {late};; esac; true'
    nested = [sys.executable, "-c", NESTED_RUN, "--no-coverage", "--mutate-all", "--max-workers", "1"]
    nested += ["--root", str(project), "--test-command", inner_tests, str(project / "demo.py")]

    result = CommandRunner().run(nested, project, 3)

    assert started.exists(), result.output
    assert result.timed_out
    assert list((project / "target" / "mutation-workers").iterdir()) == []
    wait_until(started, 5.5)
    assert not late.exists()


def test_stop_after_a_command_finished_signals_nothing_and_starts_nothing(monkeypatch, tmp_path):
    runner = CommandRunner()
    runner.run("true", tmp_path, 5)
    sent = []
    monkeypatch.setattr("mutator.runner._kill_group", lambda *args: sent.append(args))

    runner.stop()

    assert sent == []
    with pytest.raises(Stopped):
        runner.run(f"touch {tmp_path / 'ran'}", tmp_path, 5)
    assert not (tmp_path / "ran").exists()


def test_a_reaped_leader_is_never_signalled(monkeypatch):
    sent = []
    monkeypatch.setattr("mutator.runner._kill_group", lambda *args: sent.append(args))
    finished = subprocess.Popen(["true"], start_new_session=True)
    finished.wait()
    behind_popens_back = subprocess.Popen(["true"], start_new_session=True)
    os.waitpid(behind_popens_back.pid, 0)  # reaped, but returncode stays None

    assert _signal_group(finished, signal.SIGTERM) is False
    assert _signal_group(behind_popens_back, signal.SIGTERM) is False

    assert behind_popens_back.returncode is None
    assert sent == []


def test_an_unreaped_leader_is_signalled_with_or_without_waitid(monkeypatch):
    sent = []
    monkeypatch.setattr("mutator.runner._kill_group", lambda *args: sent.append(args))
    running = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        assert _signal_group(running, signal.SIGTERM) is True
        monkeypatch.delattr("mutator.runner.os.waitid")  # macOS before Python 3.13
        assert _signal_group(running, signal.SIGKILL) is True
    finally:
        running.kill()
        running.wait()

    assert sent == [(running.pid, signal.SIGTERM), (running.pid, signal.SIGKILL)]
