import shlex
import signal
import sys
import time

import pytest

from mutator.runner import CommandRunner, _clojure_command, _end_group, _kill_group, _python_command, nearest
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
    for group in (1, 0, -7):
        with pytest.raises(ValueError):
            _kill_group(group)
    assert sent == []
    _kill_group(4242)
    assert sent == [(4242, signal.SIGKILL)]


def test_nothing_the_command_started_outlives_the_run(tmp_path):
    late = tmp_path / "late.txt"
    command = f"(sleep 0.3; echo late > {shlex.quote(str(late))}) >/dev/null 2>&1 &"
    result = CommandRunner().run(command, tmp_path, 5)
    assert result.code == 0
    time.sleep(0.6)
    assert not late.exists()


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
