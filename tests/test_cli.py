import re
import shlex
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from mutator.cli import parse_args, run
from mutator.crapper_link import ensure_crapper
from mutator.runner import CommandResult


def test_lines_option_parses_positive_numbers():
    options = parse_args(["--lines", "2, 4", "src/demo.py"])
    assert options.lines == {2, 4}


def test_changed_selects_a_dirty_source_file(tmp_path, monkeypatch, capsys):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "add", "src/demo.py"], cwd=tmp_path, check=True, capture_output=True)
    monkeypatch.chdir(tmp_path)
    code = run(["--scan", "--no-coverage", "--changed", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "demo.py:2" in captured.out


def test_a_directory_argument_skips_a_named_test_file(tmp_path, capsys):
    src = tmp_path / "pkg" / "app.py"
    src.parent.mkdir()
    src.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    test = tmp_path / "pkg" / "test_app.py"
    test.write_text("def test_place():\n    assert True\n", encoding="utf-8")
    code = run(
        ["--scan", "--no-coverage", "--root", str(tmp_path), str(tmp_path / "pkg"), str(test)]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Skipping test file" in captured.err
    assert "app.py:2" in captured.out


def test_help_and_conflicting_flags():
    help_options = parse_args(["--help"])
    assert help_options.exit_code == 0
    assert "uml-viewer" in help_options.message
    assert "__pycache__" in help_options.message
    assert "testdata" in help_options.message
    conflict = parse_args(["--mutate-all", "--since-last-run", "src/demo.py"])
    assert conflict.exit_code == 1
    assert run(["--help"]) == 0


def test_scan_lists_sites_without_writing_metrics(tmp_path, monkeypatch):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    code = run(["--scan", "--no-coverage", "--root", str(tmp_path), "src/demo.py"])
    assert code == 0
    assert not (tmp_path / ".metrics").exists()


def test_real_python_mutants_update_the_snapshot(tmp_path, capsys):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(
        "def add(a, b):\n"
        "    if a > 0:\n"
        "        return a + b\n"
        "    return 0\n",
        encoding="utf-8",
    )
    command = (
        f"{sys.executable} -c \"import sys; sys.path.insert(0, 'src'); "
        "from demo import add; raise SystemExit(0 if add(2, 3) == 5 else 1)\""
    )
    code = run(
        [
            "--no-coverage",
            "--mutate-all",
            "--root",
            str(tmp_path),
            "--test-command",
            command,
            "src/demo.py",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "SURVIVED" in captured.out
    assert "KILLED" in captured.out
    snapshot = tmp_path / ".metrics" / "mutate" / "demo.edn"
    assert snapshot.is_file()
    text = snapshot.read_text(encoding="utf-8")
    assert ":namespace \"demo\"" in text
    assert ":id \"defn/add\"" in text
    assert path.read_text(encoding="utf-8").startswith("def add")


def test_a_failed_coverage_command_does_not_read_the_report(tmp_path, capsys):
    path = tmp_path / "src" / "app.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    report = tmp_path / "target" / "coverage" / "python" / "lcov.info"
    report.parent.mkdir(parents=True)
    report.write_text("SF:src/app.py\nDA:1,1\nDA:2,1\nend_of_record\n", encoding="utf-8")
    code = run(
        [
            "--root",
            str(tmp_path),
            "--coverage-command",
            "exit 3",
            "--test-command",
            "false",
            "src/app.py",
        ]
    )
    captured = capsys.readouterr()
    assert code == 3
    assert "will not be read" in captured.err
    assert "Baseline failed" not in captured.err
    assert path.read_text(encoding="utf-8").startswith("def place")


def _demo(tmp_path):
    (tmp_path / "demo.py").write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    return ["--no-coverage", "--root", str(tmp_path), "demo.py"]


def test_sigterm_during_a_run_exits_143_and_the_old_handler_comes_back(tmp_path, monkeypatch):
    exits = []

    def mutate(options, root, files, reports):
        handler = signal.getsignal(signal.SIGTERM)
        with pytest.raises(SystemExit) as stop:
            handler(signal.SIGTERM, None)
        exits.append(stop.value.code)
        return 0

    monkeypatch.setattr("mutator.cli._mutate_files", mutate)
    before = signal.getsignal(signal.SIGTERM)

    assert run(_demo(tmp_path)) == 0
    assert exits == [143]
    assert signal.getsignal(signal.SIGTERM) is before


def test_a_run_off_the_main_thread_works_and_installs_no_handler(tmp_path, monkeypatch):
    seen = []

    def mutate(options, root, files, reports):
        seen.append(signal.getsignal(signal.SIGTERM))
        return 0

    monkeypatch.setattr("mutator.cli._mutate_files", mutate)
    before = signal.getsignal(signal.SIGTERM)
    codes = []
    thread = threading.Thread(target=lambda: codes.append(run(_demo(tmp_path))))
    thread.start()
    thread.join()

    assert codes == [0]
    assert seen == [before]


_PY_TOML = "[tool.pytest.ini_options]\npythonpath = ['src']\n"
_PY_DEMO = "def tick():\n    return 1 == 1\n\n\ndef tock():\n    return 2 == 2\n"
_RS_LIB = "pub fn tick() -> i32 {\n    1\n}\n\npub fn tock() -> i32 {\n    1\n}\n"
_GO_CLOCK = "package clock\n\nfunc Tick() int {\n\treturn 1\n}\n\nfunc Tock() int {\n\treturn 1\n}\n"
_JAVA_CLOCK = (
    "package demo;\n\npublic class Clock {\n    public int tick() {\n        return 1;\n    }\n\n"
    "    public int tock() {\n        return 1;\n    }\n}\n"
)
# JaCoCo: tick's return (line 5) executed, tock's (line 9) missed.
_JACOCO = (
    '<report name="demo"><package name="demo"><sourcefile name="Clock.java">'
    '<line nr="5" mi="0" ci="2" mb="0" cb="0"/><line nr="9" mi="2" ci="0" mb="0" cb="0"/>'
    "</sourcefile></package></report>\n"
)
# A report this run's tools didn't write: every line of the file hit.
_PY_LEFTOVER = "SF:src/demo.py\nDA:1,1\nDA:2,1\nDA:5,1\nDA:6,1\nend_of_record\n"
_PY_FRESH = "SF:src/demo.py\nDA:1,1\nDA:2,1\nDA:5,0\nDA:6,0\nend_of_record\n"
_PY_PROJECT = {"pyproject.toml": _PY_TOML, "src/demo.py": _PY_DEMO, "coverage/lcov.info": _PY_LEFTOVER}


def _coverage_tools(root: Path, fresh: dict[str, str]):
    """Each coverage tool, faked at the process boundary: in module folder `m`, it writes
    `fresh[m]` where its command line says. `{root}` in the text is the project folder's name.
    """

    def shell(command, cwd):
        if command[1:4] == ["-m", "coverage", "run"]:
            Path(command[4].split("=", 1)[1]).touch()
            return 0
        module = Path(cwd).resolve()
        module = module.relative_to(root.resolve()).as_posix() if module.is_relative_to(root.resolve()) else None
        if module not in fresh:
            return 0
        if command[:2] == ["go", "test"]:
            report = Path(command[-1].split("=", 1)[1])
        elif command[0] == "mvn":
            report = Path(cwd) / "target" / "site" / "jacoco" / "jacoco.xml"
        elif command[1:4] == ["-m", "coverage", "lcov"]:
            report = Path(command[-1])
        else:
            return 0
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(fresh[module].replace("{root}", root.name), encoding="utf-8")
        return 0

    return shell


@pytest.mark.parametrize(
    ("files", "fresh", "args", "want"),
    [
        pytest.param(
            _PY_PROJECT, {".": _PY_FRESH}, [], {"src/demo.py": ({2}, {6})}, id="py-leftover"
        ),
        pytest.param(
            _PY_PROJECT,
            {".": _PY_FRESH},
            ["--use-existing-coverage"],
            {"src/demo.py": ({2, 6}, set())},
            id="py-leftover-existing",
        ),
        pytest.param(
            {
                "Cargo.toml": "[package]\nname = 'clock'\n",
                "src/lib.rs": _RS_LIB,
                "coverage/lcov.info": "SF:src/lib.rs\nDA:5,1\nDA:6,1\nDA:7,1\nend_of_record\n",
            },
            {},
            [],
            {"src/lib.rs": (set(), {2, 6})},
            id="rust-no-tool",
        ),
        pytest.param(
            {
                "one/pyproject.toml": _PY_TOML,
                "one/src/core.py": "def work():\n    return 1 == 1\n",
                "two/pyproject.toml": _PY_TOML,
                "two/src/core.py": "def work():\n    return 1 == 1\n",
            },
            {
                "one": "SF:src/core.py\nDA:1,1\nDA:2,1\nend_of_record\n",
                "two": "SF:src/core.py\nDA:1,0\nDA:2,0\nend_of_record\n",
            },
            [],
            {"one/src/core.py": ({2}, set()), "two/src/core.py": (set(), {2})},
            id="py-two-pkgs",
        ),
        pytest.param(
            {"a/b/c/go.mod": "module {root}/a/b/c\n", "a/b/c/clock.go": _GO_CLOCK},
            {"a/b/c": "mode: set\n{root}/a/b/c/clock.go:3.17,5.2 1 1\n{root}/a/b/c/clock.go:7.17,9.2 1 0\n"},
            [],
            {"a/b/c/clock.go": ({4}, {8})},
            id="go-deep-module",
        ),
        pytest.param(
            {"clock.go": _GO_CLOCK, "coverage.out": "mode: set\n{root}/clock.go:3.17,5.2 1 1\n"},
            {},
            [],
            {"clock.go": (set(), {4, 8})},
            id="go-no-module",
        ),
        pytest.param(
            {"a/b/c/pom.xml": "<project/>\n", "a/b/c/src/main/java/demo/Clock.java": _JAVA_CLOCK},
            {"a/b/c": _JACOCO},
            [],
            {"a/b/c/src/main/java/demo/Clock.java": ({5}, {9})},
            id="java-deep-module",
        ),
        pytest.param(
            {
                "m1/pom.xml": "<project/>\n",
                "m1/src/main/java/demo/Clock.java": _JAVA_CLOCK,
                "m2/pom.xml": "<project/>\n",
                "m2/src/main/java/demo/Clock.java": _JAVA_CLOCK,
            },
            {"m1": _JACOCO.replace('ci="2"', 'ci="0"'), "m2": _JACOCO},
            [],
            {"m1/src/main/java/demo/Clock.java": (set(), {5, 9}), "m2/src/main/java/demo/Clock.java": ({5}, {9})},
            id="java-two-modules",
        ),
        pytest.param(
            {"src/main/java/demo/Clock.java": _JAVA_CLOCK, "target/site/jacoco/jacoco.xml": _JACOCO},
            {},
            [],
            {"src/main/java/demo/Clock.java": (set(), {5, 9})},
            id="java-no-pom",
        ),
    ],
)
def test_a_default_run_reads_only_the_reports_this_run_wrote(tmp_path, monkeypatch, capsys, files, fresh, args, want):
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text.replace("{root}", tmp_path.name), encoding="utf-8")
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "run_shell", _coverage_tools(tmp_path, fresh))
    monkeypatch.setattr(runners.shutil, "which", lambda _name: None)
    sources = [str(tmp_path / relative) for relative in want]
    command = ["--root", str(tmp_path), "--mutate-all", "--max-workers", "1", "--test-command", "true"]
    run([*command, *args, *sources])
    got: dict[str, tuple[set[int], set[int]]] = {relative: (set(), set()) for relative in want}
    for status, relative, line in re.findall(r"^(SURVIVED|UNCOVERED) +(\S+):(\d+) ", capsys.readouterr().out, re.M):
        got[relative][status == "UNCOVERED"].add(int(line))
    assert got == want
    for relative in files:
        assert (tmp_path / relative).is_file(), f"{relative} was deleted"


def test_a_run_nested_in_its_own_worker_is_refused_before_it_runs_a_command(tmp_path, capsys):
    # Issue #57: a mutant in mutator's own tests can start a run with no --root
    # inside the worker it is tested in. That run's tests start the next one.
    project = tmp_path / "project"
    project.mkdir()
    (project / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    marker = tmp_path / "nested-ran"
    nested = [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "1"]
    nested += ["--test-command", f"touch {shlex.quote(str(marker))}", "demo.py"]
    test = f'case "$PWD" in */target/mutation-workers/*) exec {shlex.join(nested)};; esac; exit 0'

    code = run(["--root", str(project), "--no-coverage", "--mutate-all", "--max-workers", "1", "--test-command", test, "demo.py"])

    err = capsys.readouterr().err
    assert code == 2
    assert "Unmutated tests failed in a mutation worker for demo.py" in err
    assert re.search(r"mutator does not run in \S+/target/mutation-workers/run-[^/]+/worker-0: it is inside", err), err
    assert not marker.exists()


def _worker_tree(tmp_path):
    worker = tmp_path / "project" / "target" / "mutation-workers" / "run-1" / "worker-0"
    (worker / "src").mkdir(parents=True)
    (tmp_path / "link").symlink_to(worker)
    return worker


@pytest.mark.parametrize(
    "where",
    ["project/target/mutation-workers", "project/target/mutation-workers/run-1/worker-0", "project/target/mutation-workers/run-1/worker-0/src", "link"],
)
def test_a_root_in_target_mutation_workers_is_refused(tmp_path, capsys, where):
    _worker_tree(tmp_path)
    root = tmp_path / where
    (root / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    marker = tmp_path / "ran"
    refusal = f"mutator does not run in {root.resolve()}: it is inside target/mutation-workers"

    code = run(["--root", str(root), "--no-coverage", "--mutate-all", "--test-command", f"touch {marker}", "demo.py"])
    out, err = capsys.readouterr()
    scan = run(["--scan", "--no-coverage", "--root", str(root), "demo.py"])
    scan_out, scan_err = capsys.readouterr()

    assert (code, scan) == (1, 1)
    assert refusal in err
    assert refusal in scan_err
    assert out == scan_out == ""
    assert not marker.exists()
    assert not (root / ".metrics").exists()
    assert not (root / "target").exists()
    assert run(["--root", str(root), "--help"]) == 0


@pytest.mark.parametrize("where", ["project/target/app", "project/mutation-workers/app"])
def test_a_root_beside_target_mutation_workers_runs(tmp_path, capsys, where):
    _worker_tree(tmp_path)
    root = tmp_path / where
    root.mkdir(parents=True)
    (root / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    code = run(["--scan", "--no-coverage", "--root", str(root), "demo.py"])

    assert code == 0
    assert "demo.py:2 1 -> 0" in capsys.readouterr().out
