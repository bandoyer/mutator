import json
import shlex
import sys
from pathlib import Path

import pytest

from mutator.cli import parse_args, run
from mutator.edn import loads


@pytest.fixture
def python_project(tmp_path):
    pytest.importorskip("mutmut")
    pytest.importorskip("tomli_w")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["lib"]\n'
    )
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "demo.py").write_text(
        'def add(x):\n    return x + 1\n\n'
        'class Box:\n    def _value(self, x):\n        return x > 0\n\n'
        'def untouched(x):\n    return x + 2\n'
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_demo.py").write_text(
        'from pathlib import Path\nimport demo\n'
        'def test_add():\n    assert demo.add(2) == 3\n'
        'def test_box():\n    assert demo.Box()._value(1)\n'
        'def test_layout():\n'
        '    assert (Path(demo.__file__).parent / "command").is_symlink()\n'
        '    assert (Path(demo.__file__).parent / "command").resolve() == Path(demo.__file__).resolve()\n'
    )
    (tmp_path / "lib" / "command").symlink_to("demo.py")
    return tmp_path


def drive(project, *args):
    return run(["--root", str(project), "--python-backend", "mutmut", "--max-workers", "2",
                "--test-command", shlex.join([sys.executable, "-m", "pytest", "-q"]),
                *args, "lib/demo.py"])


def test_backend_is_opt_in_and_invalid_names_are_rejected():
    assert parse_args([]).python_backend == "native"
    assert parse_args(["--python-backend", "unknown"]).exit_code == 1


def test_real_backend_preserves_reports_sources_import_roots_and_symlinks(python_project, capsys):
    project = python_project
    original = (project / "lib" / "demo.py").read_bytes()
    config = (project / "pyproject.toml").read_bytes()
    code = drive(project)
    captured = capsys.readouterr()
    assert code == 3, captured.err
    assert "KILLED" in captured.out
    assert "SURVIVED" in captured.out
    assert "UNCOVERED" in captured.out
    files = list((project / ".metrics" / "mutate").glob("*.edn"))
    data = [loads(path.read_text()) for path in files]
    assert any(form["id"] == "defn-/_value" for snapshot in data for form in snapshot["forms"])
    assert all(json.loads(key)[-1] == "mutmut==3.8.0" for snapshot in data for key in snapshot["outcomes"])
    assert (project / "lib" / "demo.py").read_bytes() == original
    assert (project / "pyproject.toml").read_bytes() == config
    assert (project / "lib" / "command").readlink() == Path("demo.py")
    assert not list((project / "target" / "mutation-workers").glob("run-*"))


def test_line_selection_runs_only_the_selected_mutations(python_project, capsys):
    code = drive(python_project, "--lines", "2")
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "KILLED" in captured.out
    assert all("demo.py:2 " in line for line in captured.out.splitlines() if line.startswith("KILLED"))
    assert "SURVIVED" not in captured.out


def test_scan_runs_no_tests_and_writes_no_snapshot(python_project, capsys):
    (python_project / "tests" / "test_demo.py").write_text('raise RuntimeError("must not import")\n')
    code = run(["--root", str(python_project), "--python-backend", "mutmut", "--scan", "--lines", "2",
                "--test-command", shlex.join([sys.executable, "-m", "pytest"]), "lib/demo.py"])
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert "mutation sites" in captured.out
    assert "demo.py:2" in captured.out
    assert not (python_project / ".metrics").exists()


def test_failed_tests_leave_previous_snapshot_intact(python_project, capsys):
    snapshot = python_project / ".metrics" / "mutate" / "demo.edn"
    snapshot.parent.mkdir(parents=True)
    snapshot.write_text("previous snapshot\n")
    (python_project / "tests" / "test_demo.py").write_text("def test_failure():\n    assert False\n")
    assert drive(python_project) == 2
    assert snapshot.read_text() == "previous snapshot\n"
    assert "no Python snapshots" in capsys.readouterr().err
    assert not list((python_project / "target" / "mutation-workers").glob("run-*"))


def test_custom_shell_commands_are_not_silently_ignored(python_project, capsys):
    code = run(["--root", str(python_project), "--python-backend", "mutmut",
                "--test-command", "sh -c 'pytest -q'", "lib/demo.py"])
    assert code == 2
    assert "without shell wrappers" in capsys.readouterr().err
    assert not (python_project / ".metrics").exists()


def test_no_coverage_runs_the_suite_for_unassociated_functions(python_project, capsys):
    code = drive(python_project, "--no-coverage")
    captured = capsys.readouterr()
    assert code == 3, captured.err
    assert "SURVIVED  lib/demo.py:9" in captured.out
    assert "UNCOVERED" not in captured.out


def test_a_test_edit_invalidates_prior_kills(python_project, capsys):
    assert drive(python_project, "--lines", "2") == 0
    capsys.readouterr()
    test = python_project / "tests" / "test_demo.py"
    test.write_text(test.read_text().replace("assert demo.add(2) == 3", "demo.add(2)"))
    assert drive(python_project, "--lines", "2") == 3
    assert "SURVIVED" in capsys.readouterr().out


def test_backend_timeout_leaves_no_snapshot_or_workers(python_project, capsys):
    assert drive(python_project, "--baseline-timeout", "0.01") == 2
    assert "no Python snapshots" in capsys.readouterr().err
    assert not (python_project / ".metrics").exists()
    assert not list((python_project / "target" / "mutation-workers").glob("run-*"))


def test_no_selected_sites_still_requires_clean_tests(python_project, capsys):
    (python_project / "tests" / "test_demo.py").write_text("def test_failure():\n    assert False\n")
    assert drive(python_project, "--lines", "999") == 2
    assert "clean tests failed" in capsys.readouterr().err
    assert not (python_project / ".metrics").exists()


def test_unsupported_mutmut_config_is_not_silently_discarded(python_project, capsys):
    config = python_project / "pyproject.toml"
    config.write_text(config.read_text() + '\n[tool.mutmut]\nmax_stack_depth = 1\n')
    assert drive(python_project) == 2
    assert "cannot preserve" in capsys.readouterr().err
    assert not (python_project / ".metrics").exists()


def test_unicode_and_multiline_replacements_keep_byte_offsets(python_project, capsys):
    source = python_project / "lib" / "demo.py"
    source.write_text('def add(x):\n    return "héllo" if x else "你好"\n')
    (python_project / "tests" / "test_demo.py").write_text(
        'import demo\ndef test_add():\n    assert demo.add(1) == "héllo"\n'
    )
    assert drive(python_project) == 3, capsys.readouterr().err
    captured = capsys.readouterr()
    assert "KILLED" in captured.out
    assert "SURVIVED" in captured.out


def test_backend_does_not_score_unknown_or_partial_outcomes():
    pytest.importorskip("mutmut")
    from mutator.mutmut_bridge import outcomes
    from mutmut.mutation.data import SourceFileMutationData
    from unittest.mock import patch

    for code in (None, 2, 3, 34, 35, -11):
        def load(data):
            data.exit_code_by_key = {"demo.x_add__mutmut_1": code}

        with patch.object(SourceFileMutationData, "load", load), pytest.raises(ValueError):
            outcomes({"demo.py": [{"key": "demo.x_add__mutmut_1"}]})
