import collections
import py_compile
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
from mutator.workers import new_run_dir


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
            {"src/lib.rs": (set(), set())},
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
            {"clock.go": (set(), set())},
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
            {"src/main/java/demo/Clock.java": (set(), set())},
            id="java-no-pom",
        ),
    ],
)
def test_a_default_run_reads_only_the_reports_this_run_wrote(tmp_path, monkeypatch, capsys, files, fresh, args, want):
    # A file no report lists stops with no site line (#12), so the absence of a SURVIVED
    # line is what shows its leftover report was not read.
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


_ALPHA = "def work():\n    return 1 == 1\n"
_TS_CLOCK = "export function tick(): number {\n  return 1;\n}\n"


@pytest.mark.parametrize(
    ("files", "fresh", "args", "ran", "stopped"),
    [
        pytest.param(
            {"one/pyproject.toml": _PY_TOML, "one/src/alpha.py": _ALPHA, "two/pyproject.toml": _PY_TOML, "two/src/beta.py": _ALPHA},
            {"one": "SF:src/alpha.py\nDA:1,1\nDA:2,1\nend_of_record\n"},
            [],
            {"one/src/alpha.py": {2}},
            ["two/src/beta.py"],
            id="py-one-of-two",
        ),
        pytest.param({"pyproject.toml": _PY_TOML, "src/demo.py": _PY_DEMO}, {}, [], {}, ["src/demo.py"], id="py-no-report"),
        pytest.param({"Cargo.toml": "[package]\nname = 'clock'\n", "src/lib.rs": _RS_LIB}, {}, [], {}, ["src/lib.rs"], id="rust-no-tool"),
        pytest.param({"clock.go": _GO_CLOCK}, {}, [], {}, ["clock.go"], id="go-no-module"),
        pytest.param({"src/demo/core.clj": "(ns demo.core)\n\n(defn tick []\n  (= 1 1))\n"}, {}, [], {}, ["src/demo/core.clj"], id="clojure-no-deps"),
        pytest.param(
            {"pom.xml": "<project/>\n", "src/main/java/demo/Clock.java": _JAVA_CLOCK},
            {},
            [],
            {},
            ["src/main/java/demo/Clock.java"],
            id="java-no-report",
        ),
        pytest.param(
            {"package.json": '{"scripts": {"coverage": "exit 1"}}\n', "src/clock.ts": _TS_CLOCK},
            {},
            [],
            {},
            ["src/clock.ts"],
            id="ts-no-report",
        ),
        pytest.param(
            {"pyproject.toml": _PY_TOML, "src/demo.py": _PY_DEMO},
            {},
            ["--use-existing-coverage"],
            {},
            ["src/demo.py"],
            id="existing-no-report",
        ),
        pytest.param(
            {"src/demo.py": _PY_DEMO, "coverage/lcov.info": "SF:src/other.py\nDA:1,1\nend_of_record\n"},
            {},
            ["--coverage-command", "true"],
            {},
            ["src/demo.py"],
            id="command-report-omits-file",
        ),
    ],
)
def test_a_file_no_coverage_report_lists_stops_with_exit_2_and_runs_none_of_its_sites(
    tmp_path, monkeypatch, capsys, files, fresh, args, ran, stopped
):
    # Issue #12: coverage measured nothing for the file, so mutator can't tell which
    # sites the tests reach. The file must not be scored as if every site were uncovered.
    for relative, text in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "run_shell", _coverage_tools(tmp_path, fresh))
    monkeypatch.setattr(runners.shutil, "which", lambda _name: None)
    sources = [str(tmp_path / relative) for relative in [*ran, *stopped]]
    command = ["--root", str(tmp_path), "--mutate-all", "--max-workers", "1", "--test-command", "true"]

    code = run([*command, *args, *sources])

    out, err = capsys.readouterr()
    assert code == 2
    # A site that ran prints SURVIVED under `--test-command true`; an UNCOVERED line did not run.
    sites = re.findall(r"^(SURVIVED|UNCOVERED|KILLED|TIMEOUT) +(\S+):(\d+) ", out, re.M)
    assert {(status, relative, int(line)) for status, relative, line in sites} == {
        ("SURVIVED", r, n) for r, lines in ran.items() for n in lines
    }
    snapshots = "".join(path.read_text() for path in tmp_path.glob(".metrics/mutate/**/*.edn"))
    for relative in stopped:
        assert f'"{relative}"' not in snapshots
        assert f"No coverage data for {relative}: no coverage report lists it" in err
        assert "--no-coverage" in err
    for relative in ran:
        assert f'"{relative}"' in snapshots


def test_a_file_a_report_lists_with_zero_hits_is_measured(tmp_path, monkeypatch, capsys):
    (tmp_path / "pyproject.toml").write_text(_PY_TOML, encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "demo.py").write_text(_PY_DEMO, encoding="utf-8")
    zero = "SF:src/demo.py\nDA:1,0\nDA:2,0\nDA:5,0\nDA:6,0\nend_of_record\n"
    monkeypatch.setattr(ensure_crapper().runners, "run_shell", _coverage_tools(tmp_path, {".": zero}))
    command = ["--root", str(tmp_path), "--mutate-all", "--max-workers", "1", "--test-command", "true"]

    code = run([*command, str(tmp_path / "src" / "demo.py")])

    out, err = capsys.readouterr()
    assert code == 0
    assert re.findall(r"^UNCOVERED +src/demo.py:(\d+) ", out, re.M) == ["2", "2", "2", "6"]
    assert "No coverage data" not in err


@pytest.mark.parametrize("link", [None, "target", "target/mutation-workers"])
def test_a_run_nested_in_its_own_worker_is_refused_before_it_runs_a_command(tmp_path, capsys, link):
    # Issue #57: a mutant in mutator's own tests can start a run with no --root
    # inside the worker it is tested in. That run's tests start the next one.
    # When `link` points at storage elsewhere, the worker's real path has neither name.
    project = tmp_path / "project"
    project.mkdir()
    if link:
        (project / link).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "storage").mkdir()
        (project / link).symlink_to(tmp_path / "storage")
    (project / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    marker = tmp_path / "nested-ran"
    nested = [sys.executable, "-m", "mutator", "--no-coverage", "--mutate-all", "--max-workers", "1"]
    nested += ["--test-command", f"touch {shlex.quote(str(marker))}", "demo.py"]
    test = f'case "$PWD" in */run-*/worker-*) exec {shlex.join(nested)};; esac; exit 0'

    code = run(["--root", str(project), "--no-coverage", "--mutate-all", "--max-workers", "1", "--test-command", test, "demo.py"])

    err = capsys.readouterr().err
    assert code == 2
    assert "Unmutated tests failed in a mutation worker for demo.py" in err
    assert re.search(r"mutator does not run in \S+/run-[^/]+/worker-0: it is inside", err), err
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


@pytest.mark.parametrize(("where", "expected"), [("..", 0), (".", 1), ("worker-0/src", 1)])
def test_in_relocated_storage_only_a_run_folder_is_refused(tmp_path, capsys, where, expected):
    # `target` is a symlink to storage, so the run folder's real path has no target/mutation-workers.
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / "storage").mkdir()
    (project / "target").symlink_to(tmp_path / "storage")
    root = (new_run_dir(project) / where).resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")

    code = run(["--scan", "--no-coverage", "--root", str(root), "demo.py"])

    out, err = capsys.readouterr()
    assert code == expected
    assert ("demo.py:2 1 -> 0" in out) == (expected == 0)
    assert (f"mutator does not run in {root}: it is inside" in err) == (expected == 1)


# A project whose one test command runs every script in tests/, unless skip.cfg
# names it or the command names others. Each run appends a line to runs.log,
# outside the project, so a test can count them.
_KEPT_DEMO = "def f():\n    return 1\n\n\ndef h(x):\n    return x + 1\n"
_KEPT_CHECK = """import os, pathlib, sys
sys.path.insert(0, "src")
with open({log!r}, "a") as log:
    log.write("run\\n")
if os.environ.get("BREAK_CHECKS"):
    raise SystemExit(1)
skip = pathlib.Path("skip.cfg").read_text().split()
for path in sorted(pathlib.Path("tests").glob("*.py")):
    if path.stem not in skip and (len(sys.argv) < 2 or path.stem in sys.argv[1:]):
        exec(compile(path.read_text(), str(path), "exec"), {{}})
"""
_ASSERT_F = "from demo import f\nassert f() == 1\n"
_CALL_F = "from demo import f\nf()\n"


def _git(root, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *args],
        cwd=root,
        check=True,
        capture_output=True,
    )


def _kept_project(root, files, use_git=True):
    log = root.parent / f"{root.name}-runs.log"
    texts = {
        "src/demo.py": _KEPT_DEMO,
        "check.py": _KEPT_CHECK.format(log=str(log)),
        "skip.cfg": "",
        "tests/f.py": _ASSERT_F,
        "tests/h.py": "from demo import h\nassert h(1) == 2\n",
        **files,
    }
    for relative, text in texts.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    if use_git:
        _git(root, "init", "-q")
        _git(root, "add", "-A")
        _git(root, "commit", "-qm", "fixture")
    return log


def _kept_run(root, capsys, *args, command=None):
    command = command or f"{sys.executable} check.py"
    code = run(["--root", str(root), "--no-coverage", "--max-workers", "1", "--test-command", command, *args])
    return code, capsys.readouterr()


def _write(path, text):
    path.write_text(text, encoding="utf-8")


_F_KILLED = re.compile(r"^KILLED +src/demo\.py:2 1 -> 0$", re.M)
_F_SURVIVED = re.compile(r"^SURVIVED +src/demo\.py:2 1 -> 0$", re.M)


@pytest.mark.parametrize(
    "files, change, args, use_git",
    [
        pytest.param(
            {},
            lambda root: (_write(root / "tests/f.py", _CALL_F), _git(root, "commit", "-qam", "weaken")),
            [],
            True,
            id="assertion-deleted",
        ),
        pytest.param({}, lambda root: (root / "tests/f.py").unlink(), [], True, id="test-file-deleted"),
        pytest.param(
            {"tests/f.py": "import demo\nassert demo.f() == 1\n"},
            lambda root: _write(root / "tests/a.py", "import demo\ndemo.f = lambda: 1\n"),
            [],
            True,
            id="untracked-file-added",
        ),
        pytest.param({}, lambda root: _write(root / "skip.cfg", "f\n"), [], True, id="config-changed"),
        pytest.param({}, lambda root: None, ["--test-command", f"{sys.executable} check.py h"], True, id="command-changed"),
        pytest.param({}, lambda root: None, ["--timeout-factor", "20"], True, id="timeout-factor-changed"),
        pytest.param({}, lambda root: None, ["--memory-limit", "4096"], True, id="memory-limit-changed"),
        pytest.param(
            {
                "src/app.py": "from demo import f\n\n\ndef g():\n    return f()\n",
                "tests/f.py": "from app import g\nassert g() == 1\n",
            },
            lambda root: _write(root / "src/app.py", "from demo import f\n\n\ndef g():\n    return 1\n"),
            [],
            True,
            id="other-source-changed",
        ),
        pytest.param(
            {
                "src/demo.py": _KEPT_DEMO + "\n\ndef check():\n    assert f() == 1\n",
                "tests/f.py": "import demo\ndemo.check()\n",
            },
            lambda root: _write(root / "src/demo.py", _KEPT_DEMO + "\n\ndef check():\n    f()\n"),
            [],
            True,
            id="same-file-test-changed",
        ),
        pytest.param({}, lambda root: _write(root / "tests/f.py", _CALL_F), [], False, id="no-git"),
    ],
)
def test_a_kept_kill_is_run_again_when_its_test_context_changed(tmp_path, capsys, files, change, args, use_git):
    root = tmp_path / "project"
    log = _kept_project(root, files, use_git)
    code, first = _kept_run(root, capsys, "src/demo.py")
    assert code == 0, first.err
    assert _F_KILLED.search(first.out)
    change(root)
    runs = log.read_text().count("run")
    code, second = _kept_run(root, capsys, *args, "src/demo.py")
    if args[:1] in (["--timeout-factor"], ["--memory-limit"]):
        # The same tests kill the mutant again, so it must be run, not kept:
        # the baseline, the worker's control run, and three mutants.
        assert code == 0, second.err
        assert _F_KILLED.search(second.out)
        assert log.read_text().count("run") - runs == 5
        return
    assert code == 3, second.err
    assert _F_SURVIVED.search(second.out)


def test_mutate_all_with_lines_drops_a_kill_on_another_line_when_the_tests_changed(tmp_path, capsys):
    root = tmp_path / "project"
    _kept_project(root, {})
    code, first = _kept_run(root, capsys, "--mutate-all", "src/demo.py")
    assert code == 0 and _F_KILLED.search(first.out)
    _write(root / "tests/f.py", _CALL_F)
    code, second = _kept_run(root, capsys, "--mutate-all", "--lines", "6", "src/demo.py")
    assert code == 0, second.err
    assert "src/demo.py:2" not in second.out
    assert re.search(r"^KILLED +src/demo\.py:6 ", second.out, re.M)


@pytest.mark.parametrize("breaks", ["command", "environment"])
def test_when_every_site_is_kept_tests_that_fail_now_stop_the_run(tmp_path, capsys, monkeypatch, breaks):
    root = tmp_path / "project"
    _kept_project(root, {})
    code, first = _kept_run(root, capsys, "src/demo.py")
    assert code == 0 and _F_KILLED.search(first.out)
    snapshot = root / ".metrics" / "mutate" / "demo.edn"
    before = (snapshot.read_bytes(), snapshot.stat().st_mtime_ns)
    command = None
    if breaks == "command":
        command = "false"
    else:
        monkeypatch.setenv("BREAK_CHECKS", "1")
    code, second = _kept_run(root, capsys, "src/demo.py", command=command)
    assert code == 2
    assert "Baseline failed for src/demo.py" in second.err
    assert "KILLED" not in second.out
    assert "Wrote" not in second.err
    assert (snapshot.read_bytes(), snapshot.stat().st_mtime_ns) == before


def test_a_kill_is_kept_when_nothing_changed_and_only_the_baseline_runs(tmp_path, capsys):
    root = tmp_path / "project"
    log = _kept_project(root, {})
    code, first = _kept_run(root, capsys, "src/demo.py")
    assert code == 0 and _F_KILLED.search(first.out)
    for _ in range(2):
        runs = log.read_text().count("run")
        code, again = _kept_run(root, capsys, "src/demo.py")
        assert code == 0, again.err
        assert _F_KILLED.search(again.out)
        assert log.read_text().count("run") - runs == 1


def test_a_kill_from_a_snapshot_without_a_context_is_run_again(tmp_path, capsys):
    # A snapshot written before contexts existed, or edited by hand.
    root = tmp_path / "project"
    log = _kept_project(root, {})
    code, first = _kept_run(root, capsys, "src/demo.py")
    assert code == 0 and _F_KILLED.search(first.out)
    snapshot = root / ".metrics" / "mutate" / "demo.edn"
    text = snapshot.read_text(encoding="utf-8")
    assert ":context" in text
    snapshot.write_text(re.sub(r':context "[0-9a-f]+"', ":context 7", text), encoding="utf-8")
    runs = log.read_text().count("run")
    code, second = _kept_run(root, capsys, "src/demo.py")
    assert code == 0, second.err
    # The baseline, the worker's control run, and three mutants.
    assert log.read_text().count("run") - runs == 5


def test_the_same_timeout_factor_spelled_another_way_keeps_the_kill(tmp_path, capsys):
    root = tmp_path / "project"
    log = _kept_project(root, {})
    code, first = _kept_run(root, capsys, "src/demo.py")
    assert code == 0 and _F_KILLED.search(first.out)
    runs = log.read_text().count("run")
    code, second = _kept_run(root, capsys, "--timeout-factor", "1e1", "src/demo.py")
    assert code == 0, second.err
    assert _F_KILLED.search(second.out)
    assert log.read_text().count("run") - runs == 1


@pytest.mark.parametrize(("limit", "status"), [("64", "KILLED"), ("0", "SURVIVED")])
def test_a_mutant_is_killed_when_it_passes_the_memory_limit_and_survives_with_none(tmp_path, capsys, limit, status):
    # Issue #58: the mutant 0 -> 1 makes the test ask for 128 MiB.
    (tmp_path / "demo.py").write_text("def size():\n    return 0\n", encoding="utf-8")
    command = f"{sys.executable} -c 'import demo; bytes(demo.size() * 128 << 20)'"
    args = ["--root", str(tmp_path), "--no-coverage", "--mutate-all", "--max-workers", "1", "--lines", "2"]
    run([*args, "--memory-limit", limit, "--test-command", command, "demo.py"])
    assert re.search(rf"^{status} +demo.py:2 0 -> 1$", capsys.readouterr().out, re.MULTILINE)


# Issue #61: coverage.py's LCOV report lists a multi-line statement at its first
# line only. _WRAPPED_LCOV is the report coverage.py 7.16.2 wrote for _WRAPPED
# when a test called every function but `untested`, and `multi_if` with a = -1.
_WRAPPED = """import functools


def call_args(a, b):
    return max(
        a + b,
        a - b,
    )


def any_match(items):
    return any(
        (item == 1 and item > 0) or item < 0
        for item in items
    )


def multi_if(a, b):
    if (a > 0 and
            b > 0):
        return max(a,
                   b - 1)
    return 0


def decorated():
    @functools.lru_cache(
        maxsize=1 + 1,
    )
    def inner(x):
        return x
    return inner(3)


def untested(a, b):
    return max(
        a + b,
        a - b,
    )


def excluded(a):
    if a > 0:
        return a
    raise ValueError(a - 1)  # pragma: no cover
"""
_WRAPPED_HITS = {1: 1, 4: 1, 5: 1, 11: 1, 12: 1, 18: 1, 19: 1, 21: 0, 23: 1, 26: 1, 27: 1, 30: 1, 31: 1, 32: 1}
_WRAPPED_HITS |= {35: 1, 36: 0, 42: 1, 43: 1, 44: 1}
_WRAPPED_LCOV = "SF:src/demo.py\n" + "".join(f"DA:{n},{hits}\n" for n, hits in _WRAPPED_HITS.items()) + "end_of_record\n"


def test_a_site_on_a_later_line_of_a_covered_python_statement_runs(tmp_path, capsys):
    # Covered: a call's arguments (6, 7), `return any(...)` (13), an `if` test's
    # second line (20), a decorator's argument (28). Not covered: a `return` inside
    # that `if` (22), an untested function (37, 38), a `# pragma: no cover` line (45).
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text(_WRAPPED, encoding="utf-8")
    report = tmp_path / "target" / "coverage" / "python" / "lcov.info"
    report.parent.mkdir(parents=True)
    report.write_text(_WRAPPED_LCOV, encoding="utf-8")
    want = {"run": {6, 7, 13, 19, 20, 23, 28, 43}, "uncovered": {22, 37, 38, 45}}
    mutate = ["--use-existing-coverage", "--mutate-all", "--max-workers", "1", "--test-command", "true"]

    assert run(["--root", str(tmp_path), *mutate, str(source)]) == 3
    got = {"run": set(), "uncovered": set()}
    for status, line in re.findall(r"^(SURVIVED|UNCOVERED) +src/demo.py:(\d+) ", capsys.readouterr().out, re.M):
        got["uncovered" if status == "UNCOVERED" else "run"].add(int(line))
    assert got == want

    assert run(["--root", str(tmp_path), "--scan", str(source)]) == 0
    got = {"run": set(), "uncovered": set()}
    for line, mark in re.findall(r"^. src/demo.py:(\d+) .*?( uncovered)?  \[", capsys.readouterr().out, re.M):
        got["uncovered" if mark else "run"].add(int(line))
    assert got == want


_TS_CLOCK = "export function tick(): boolean { return 1 === 1 }\n"
_CLJ_CORE = "(ns demo.core)\n(defn tick [] (= 1 1))\n"
# Per language: the project's files, the source to mutate, the report its tool writes, and the report's text.
_FAILED_RUNS = {
    "python": (
        {"pyproject.toml": _PY_TOML, "src/demo.py": _PY_DEMO},
        "src/demo.py",
        "target/coverage/python/lcov.info",
        _PY_FRESH,
    ),
    "go": (
        {"go.mod": "module {root}\n", "clock.go": _GO_CLOCK},
        "clock.go",
        "target/coverage/go/coverage.out",
        "mode: set\n{root}/clock.go:3.17,5.2 1 1\n{root}/clock.go:7.17,9.2 1 0\n",
    ),
    "java": (
        {"pom.xml": "<project/>\n", "src/main/java/demo/Clock.java": _JAVA_CLOCK},
        "src/main/java/demo/Clock.java",
        "target/site/jacoco/jacoco.xml",
        _JACOCO,
    ),
    "typescript": (
        {"package.json": '{"scripts": {"coverage": "sh coverage.sh"}}\n', "src/clock.ts": _TS_CLOCK},
        "src/clock.ts",
        "coverage/lcov.info",
        "SF:src/clock.ts\nDA:1,1\nend_of_record\n",
    ),
    "rust": (
        {"Cargo.toml": "[package]\nname = 'clock'\n", "src/lib.rs": _RS_LIB},
        "src/lib.rs",
        "target/coverage/rust/lcov.info",
        "SF:src/lib.rs\nDA:1,1\nDA:2,1\nDA:3,1\nend_of_record\n",
    ),
    "clojure": (
        {"deps.edn": "{}\n", "src/demo/core.clj": _CLJ_CORE},
        "src/demo/core.clj",
        "target/coverage/lcov.info",
        "SF:src/demo/core.clj\nDA:2,1\nend_of_record\n",
    ),
}


def _tools_that_exit(root: Path, text: str, codes: dict[str, int]):
    """Each coverage tool writes `text` where its command line says, then exits with `codes[<module>]`
    (0 for a module not named). coverage.py's `run` step exits with that status and its `lcov` step
    with 0, as when the tests fail under coverage. Probes such as `python -c "import coverage"` exit 0.
    """

    def shell(command, cwd):
        cwd = Path(cwd)
        relative = cwd.resolve().relative_to(root.resolve()).as_posix()
        code = codes.get(relative, 0)
        if command[1:4] == ["-m", "coverage", "run"]:
            Path(command[4].split("=", 1)[1]).touch()
            return code
        if command[1:4] == ["-m", "coverage", "lcov"]:
            report, code = Path(command[-1]), 0
        elif command[:2] == ["go", "test"]:
            report = Path(command[-1].split("=", 1)[1])
        elif command[0] == "mvn":
            report = cwd / "target" / "site" / "jacoco" / "jacoco.xml"
        elif command[:2] == ["cargo", "llvm-cov"]:
            report = Path(command[command.index("--output-path") + 1])
        elif command[:3] == ["npm", "run", "coverage"]:
            report = cwd / "coverage" / "lcov.info"
        elif command[0] == "clj":
            report = cwd / "target" / "coverage" / "lcov.info"
        else:
            return 0
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(text.replace("{root}", root.name), encoding="utf-8")
        return code

    return shell


def _failed_run_project(tmp_path, monkeypatch, language: str, codes: dict[str, int]) -> tuple[str, Path]:
    files, source, report, text = _FAILED_RUNS[language]
    for relative, body in files.items():
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.replace("{root}", tmp_path.name), encoding="utf-8")
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "run_shell", _tools_that_exit(tmp_path, text, codes))
    monkeypatch.setattr(runners.shutil, "which", lambda name: "/bin/cargo-llvm-cov" if name == "cargo-llvm-cov" else None)
    return source, tmp_path.resolve() / report


def _mutate(tmp_path, *sources: str) -> int:
    return run(["--root", str(tmp_path), "--mutate-all", "--max-workers", "1", "--test-command", "true", *sources])


_SITE = re.compile(r"^(KILLED|SURVIVED|UNCOVERED|TIMEOUT) ", re.M)


@pytest.mark.parametrize("language", list(_FAILED_RUNS))
def test_a_failed_coverage_run_that_wrote_a_report_stops_before_any_mutant_runs(tmp_path, monkeypatch, capsys, language):
    # Issue #66: a coverage run can fail and still write a report (crapper#55). Sites that
    # only the failed run would reach would read as UNCOVERED, so no mutant runs.
    source, report = _failed_run_project(tmp_path, monkeypatch, language, {".": 1})
    code = _mutate(tmp_path, source)
    captured = capsys.readouterr()
    assert code == 2
    assert not _SITE.search(captured.out)
    assert not (tmp_path / ".metrics" / "mutate").exists()
    assert f"Coverage report from a failed run: {report} (exited 1 in {tmp_path.resolve()}).\n" in captured.err
    assert "No mutant ran:" in captured.err and "--no-coverage" in captured.err


@pytest.mark.parametrize("language", list(_FAILED_RUNS))
def test_a_coverage_run_that_succeeded_is_scored(tmp_path, monkeypatch, capsys, language):
    source, _report = _failed_run_project(tmp_path, monkeypatch, language, {})
    code = _mutate(tmp_path, source)
    captured = capsys.readouterr()
    assert code == 3
    assert f"SURVIVED  {source}:" in captured.out
    assert "from a failed run" not in captured.err


@pytest.mark.parametrize("status", [1, 255, -9])
def test_any_non_zero_coverage_status_stops_the_run(tmp_path, monkeypatch, capsys, status):
    """From 1 up, or a negative status from a signal, as the run gave it."""

    source, report = _failed_run_project(tmp_path, monkeypatch, "go", {".": status})
    assert _mutate(tmp_path, source) == 2
    assert f"{report} (exited {status} in " in capsys.readouterr().err


@pytest.mark.parametrize("failing", [["two"], ["one", "two"]])
def test_one_failed_package_stops_the_whole_run(tmp_path, monkeypatch, capsys, failing):
    # Like a failed --coverage-command, a failed run stops every file, not only its own package's.
    for package in ("one", "two"):
        (tmp_path / package / "src").mkdir(parents=True)
        (tmp_path / package / "pyproject.toml").write_text(_PY_TOML, encoding="utf-8")
        (tmp_path / package / "src" / "demo.py").write_text(_PY_DEMO, encoding="utf-8")
    text = "SF:src/demo.py\nDA:1,1\nDA:2,1\nend_of_record\n"
    runners = ensure_crapper().runners
    monkeypatch.setattr(runners, "run_shell", _tools_that_exit(tmp_path, text, {name: 1 for name in failing}))
    code = _mutate(tmp_path, "one/src/demo.py", "two/src/demo.py")
    captured = capsys.readouterr()
    assert code == 2
    assert not _SITE.search(captured.out)
    named = re.findall(r"^Coverage report from a failed run: \S+/target/coverage/python/(\w+)/lcov\.info ", captured.err, re.M)
    assert named == failing
    assert captured.err.count("No mutant ran:") == 1


def test_an_older_crappers_reports_have_no_status_and_are_scored(tmp_path, monkeypatch, capsys):
    # mutator pins no crapper. Before crapper#55, a Report had only `path` and `module`.
    old_report = collections.namedtuple("Report", ["path", "module"])
    source, _report = _failed_run_project(tmp_path, monkeypatch, "python", {".": 1})
    runners = ensure_crapper().runners
    collect = runners.collect_coverage
    monkeypatch.setattr(
        runners, "collect_coverage", lambda root, files: [old_report(r.path, r.module) for r in collect(root, files)]
    )
    code = _mutate(tmp_path, source)
    captured = capsys.readouterr()
    assert code == 3
    assert f"SURVIVED  {source}:2" in captured.out
    assert "from a failed run" not in captured.err



# Issue #82. Each site of `1 + 0` is the size of the source: 1 -> 0 and 0 -> 1 must be killed, and
# + -> - leaves 1, so it survives. A mutant that ran a .pyc of the original would survive.
_OWN_SOURCE = "def f():\n    return 1 + 0\n"
_OWN_STATUSES = [("KILLED", "1 -> 0"), ("SURVIVED", "+ -> -"), ("KILLED", "0 -> 1")]
_PY = shlex.quote(sys.executable)
_PYTEST = f"{_PY} -m pytest -q -p no:cacheprovider"


def _own_source_project(tmp_path: Path, pyc: str | None) -> tuple[Path, Path]:
    """src/demo.py, its test, and, unless `pyc` is None, a .pyc of it in that invalidation mode."""

    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text(_OWN_SOURCE, encoding="utf-8")
    (tmp_path / "test_demo.py").write_text("from demo import f\n\n\ndef test_f():\n    assert f() == 1\n", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\npythonpath = ['src']\n", encoding="utf-8")
    cache = source.parent / "__pycache__" / f"demo.{sys.implementation.cache_tag}.pyc"
    if pyc is not None:
        mode = py_compile.PycInvalidationMode[pyc.upper().replace("-", "_")]
        py_compile.compile(str(source), cfile=str(cache), invalidation_mode=mode, doraise=True)
    return source, cache


def _own_source_command(kind: str, source: Path) -> str:
    """The test command. `same-mtime` gives the worker's demo.py the project's mtime, as when a
    mutant is written in the same second. `writes` turns bytecode writing on in a worker only, and
    gives every write one mtime; mutator's baseline in the project writes nothing.
    """

    if kind == "same-mtime":
        utime = f"import os; s = os.stat({str(source)!r}); os.utime('src/demo.py', ns=(s.st_atime_ns, s.st_mtime_ns))"
        return f"{_PY} -c {shlex.quote(utime)} && {_PYTEST}"
    if kind == "writes":
        utime = "import os; os.utime('src/demo.py', (1700000000, 1700000000))"
        in_worker = f"{_PY} -c {shlex.quote(utime)} && PYTHONDONTWRITEBYTECODE= {_PYTEST}"
        return f"case $(pwd -P) in */target/mutation-workers/*) {in_worker} ;; *) {_PYTEST} ;; esac"
    return _PYTEST


@pytest.mark.parametrize(
    ("pyc", "kind"),
    [
        pytest.param("timestamp", "same-mtime", id="original-timestamp-pyc-same-second"),
        pytest.param("unchecked-hash", "plain", id="original-unchecked-hash-pyc"),
        pytest.param(None, "writes", id="test-command-writes-bytecode"),
        pytest.param("timestamp", "writes", id="test-command-writes-bytecode-beside-the-projects"),
        pytest.param("checked-hash", "plain", id="control-checked-hash-pyc"),
        pytest.param(None, "same-mtime", id="control-same-mtime-no-pyc"),
        pytest.param(None, "plain", id="control-no-pyc"),
    ],
)
def test_a_mutant_runs_its_own_source_and_leaves_the_projects_bytecode_alone(tmp_path, capsys, pyc, kind):
    source, cache = _own_source_project(tmp_path, pyc)
    before = cache.read_bytes() if pyc else None
    command = _own_source_command(kind, source)
    code = run(["--root", str(tmp_path), "--no-coverage", "--mutate-all", "--max-workers", "1", "--test-command", command, str(source)])
    out = capsys.readouterr().out
    assert re.findall(r"^(KILLED|SURVIVED) +src/demo.py:2 (.+)$", out, re.M) == _OWN_STATUSES
    assert code == 3
    if pyc:
        assert cache.read_bytes() == before
