import os
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import wait_until

from mutator.engine import BASELINE_TIMEOUT, check_backups, mutate_file, project_files_digest
from mutator.edn import loads
from mutator.metrics import snapshot_path
from mutator.runner import CommandResult, CommandRunner
from mutator.workers import create_workers, delete_tree, new_run_dir, worker_count


class FakeRunner:
    def __init__(self):
        self.verbose = False
        self.calls = 0
        self._lock = threading.Lock()

    def run(self, command, cwd, timeout):
        with self._lock:
            self.calls += 1
        text = (cwd / "src" / "demo.py").read_text(encoding="utf-8")
        namespace: dict = {}
        exec(text, namespace)
        ok = namespace["add"](1, 2) == 3
        return CommandResult(code=0 if ok else 1, timed_out=False, seconds=0.01, output="")


SOURCE = """def add(a, b):
    if a > 0:
        return a + b
    return a + b
"""


def test_mutants_are_killed_or_kept_and_the_snapshot_is_differential(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    original = path.read_bytes()
    # A kill is kept only while the files git lists are unchanged.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    runner = FakeRunner()
    first = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        files_digest=project_files_digest(tmp_path),
    )
    assert path.read_bytes() == original
    assert first.baseline_failed is False
    form = first.forms[0]
    assert form.id == "defn/add"
    assert form.namespace == "demo"
    # `>` and the unexecuted `+` survive. The executed `+` is killed. `0` -> `1`
    # still returns 3 for add(1, 2).
    assert form.killed == 1
    assert form.survived == 3
    assert form.sites == 4
    assert form.killed + form.survived + form.uncovered == form.sites
    data = loads(snapshot_path(tmp_path, "demo").read_text(encoding="utf-8"))
    assert data["namespace"] == "demo"
    assert data["forms"][0]["id"] == "defn/add"
    assert data["forms"][0]["killed"] == 1
    assert data["forms"][0]["survived"] == 3
    first_calls = runner.calls

    second = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=False,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=1,
        files_digest=project_files_digest(tmp_path),
    )
    assert second.forms[0].killed == 1
    assert second.forms[0].survived == 3
    # Baseline, the worker's control run, and the three survivors. The killed
    # mutant stays in the snapshot.
    assert runner.calls - first_calls == 5


def test_uncovered_sites_are_not_executed(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    runner = FakeRunner()
    result = mutate_file(
        path,
        tmp_path,
        runner=runner,
        covered_lines=set(),
        ignore_coverage=False,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert runner.calls == 0
    assert result.forms[0].uncovered == 4
    assert result.forms[0].killed == 0
    assert result.forms[0].sites == 4


def test_a_failed_baseline_does_not_rewrite_metrics(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")

    class Red:
        verbose = False

        def run(self, command, cwd, timeout):
            return CommandResult(code=1, timed_out=False, seconds=0.01, output="nope")

    result = mutate_file(
        path,
        tmp_path,
        runner=Red(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
    )
    assert result.baseline_failed
    assert result.baseline_message.startswith("Baseline failed")
    assert not snapshot_path(tmp_path, "demo").exists()


def test_worker_count_follows_sites_cores_and_the_requested_cap():
    cores = os.cpu_count() or 1
    assert worker_count(10, None) == min(10, cores)
    assert worker_count(10, 3) == min(3, cores)
    assert worker_count(2, 8) == min(2, cores)


def test_workers_keep_a_private_copy_and_link_the_rest(tmp_path):
    source = tmp_path / "src" / "demo.py"
    source.parent.mkdir()
    source.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    sibling = tmp_path / "src" / "other.py"
    sibling.write_text("kept = True\n", encoding="utf-8")
    marker = tmp_path / "tests" / "keep.txt"
    marker.parent.mkdir()
    marker.write_text("keep", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    original = source.read_bytes()

    base = new_run_dir(tmp_path)
    try:
        workers = create_workers(base, tmp_path, "src/demo.py", original, 2)
        assert len(workers) == 2
        for worker in workers:
            private = worker / "src" / "demo.py"
            assert private.is_file()
            assert not private.is_symlink()
            assert private.read_bytes() == original
            assert (worker / "src" / "other.py").is_symlink()
            assert (worker / "tests").is_symlink()
            assert (worker / "tests" / "keep.txt").read_text(encoding="utf-8") == "keep"
            assert (worker / "pyproject.toml").is_file()
            assert not (worker / "pyproject.toml").is_symlink()
        (workers[0] / "src" / "demo.py").write_text("changed\n", encoding="utf-8")
        assert workers[1].joinpath("src/demo.py").read_bytes() == original
        assert source.read_bytes() == original
    finally:
        delete_tree(base)

    assert source.read_bytes() == original
    assert sibling.read_text(encoding="utf-8") == "kept = True\n"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert not base.exists()


def test_node_imports_the_worker_copy_through_a_relative_specifier(tmp_path):
    source = tmp_path / "src" / "find.mjs"
    source.parent.mkdir()
    source.write_text('export const value = "real"\n', encoding="utf-8")
    beside = tmp_path / "src" / "find.test.mjs"
    beside.write_text('import { value } from "./find.mjs";\nconsole.log(value)\n', encoding="utf-8")
    (tmp_path / "src" / "notes.mjs").write_text("export const notes = 1\n", encoding="utf-8")
    main = tmp_path / "src" / "main.mjs"
    main.write_text('import { value } from "./find.mjs";\nexport const main = value\n', encoding="utf-8")
    (tmp_path / "src" / "app.test.mjs").write_text('import { main } from "./main.mjs";\n', encoding="utf-8")
    distant = tmp_path / "tests" / "use.mjs"
    distant.parent.mkdir()
    distant.write_text('import { value } from "../src/find.mjs";\nconsole.log(value)\n', encoding="utf-8")
    (tmp_path / "tests" / "other.test.mjs").write_text("export const n = 1\n", encoding="utf-8")

    base = new_run_dir(tmp_path)
    try:
        worker = create_workers(base, tmp_path, "src/find.mjs", source.read_bytes(), 1)[0]
        private = worker / "src" / "find.mjs"
        private.write_text('export const value = "worker"\n', encoding="utf-8")
        for relative in ("src/find.test.mjs", "src/main.mjs", "src/app.test.mjs", "tests/use.mjs"):
            copied = worker / relative
            assert copied.is_file()
            assert not copied.is_symlink()
        assert (worker / "src" / "notes.mjs").is_symlink()
        assert not (worker / "tests").is_symlink()
        assert (worker / "tests" / "other.test.mjs").is_symlink()
        if shutil.which("node"):
            for script in ("src/find.test.mjs", "tests/use.mjs"):
                completed = subprocess.run(
                    ["node", script],
                    cwd=worker,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                assert completed.returncode == 0
                assert completed.stdout.strip() == "worker"
    finally:
        delete_tree(base)

    assert beside.read_text(encoding="utf-8").startswith("import")
    assert distant.read_text(encoding="utf-8").startswith("import")
    assert source.read_text(encoding="utf-8") == 'export const value = "real"\n'


def test_a_mutation_run_leaves_the_project_tree_unchanged(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text("def place(x):\n    return x > 0\n", encoding="utf-8")
    original = path.read_text(encoding="utf-8")

    class Record:
        verbose = False

        def run(self, command, cwd, timeout):
            if timeout != BASELINE_TIMEOUT:
                assert "mutation-workers" in cwd.as_posix()
                assert (cwd / "src" / "demo.py").read_text(encoding="utf-8") != original
            return CommandResult(code=0 if timeout == BASELINE_TIMEOUT else 1, timed_out=False, seconds=0.01, output="")

    mutate_file(
        path,
        tmp_path,
        runner=Record(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=2,
    )
    assert path.read_text(encoding="utf-8") == original
    assert [item for item in (tmp_path / "target").rglob("*") if item.is_file()] == []


def test_an_edit_saved_while_mutants_run_is_kept(tmp_path):
    path = tmp_path / "src" / "demo.py"
    path.parent.mkdir()
    path.write_text(SOURCE, encoding="utf-8")
    edited = SOURCE + "# user edit\n"

    class Editor(FakeRunner):
        def run(self, command, cwd, timeout):
            if "mutation-workers" in cwd.parts:
                path.write_text(edited, encoding="utf-8")
            return super().run(command, cwd, timeout)

    mutate_file(
        path,
        tmp_path,
        runner=Editor(),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command="fake",
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=1,
    )
    assert path.read_text(encoding="utf-8") == edited


def test_a_backup_equal_to_its_source_is_deleted_and_one_that_differs_is_kept(tmp_path):
    base = tmp_path / "target" / "mutator-backup"
    same = tmp_path / "src" / "same.py"
    newer = tmp_path / "src" / "newer.py"
    same.parent.mkdir()
    same.write_text("original\n", encoding="utf-8")
    newer.write_text("newer edit\n", encoding="utf-8")
    (base / "src").mkdir(parents=True)
    (base / "src" / "same.py").write_text("original\n", encoding="utf-8")
    (base / "src" / "newer.py").write_text("stale backup\n", encoding="utf-8")
    (base / "gone.py").write_text("backup of a deleted file\n", encoding="utf-8")

    differing = check_backups(tmp_path)

    assert differing == [(base / "gone.py", tmp_path / "gone.py"), (base / "src" / "newer.py", newer)]
    assert not (base / "src" / "same.py").exists()
    assert same.read_text(encoding="utf-8") == "original\n"
    assert newer.read_text(encoding="utf-8") == "newer edit\n"
    assert (base / "src" / "newer.py").read_text(encoding="utf-8") == "stale backup\n"
    assert not (tmp_path / "gone.py").exists()


# Workers don't share the project's target/. This command is slow until its
# target/ holds a marker, the way a cold Cargo build is slow until target/ is built.
COLD_BUILD = "test -f target/warm || { sleep 3; mkdir -p target && touch target/warm; }"


def _mutate_with(tmp_path, test_command, verbose=False, **limits):
    path = tmp_path / "demo.py"
    path.write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "warm").touch()
    return mutate_file(
        path,
        tmp_path,
        runner=CommandRunner(verbose=verbose),
        covered_lines=None,
        ignore_coverage=True,
        mutate_all=True,
        lines=None,
        test_command=test_command,
        timeout_factor=10,
        mutation_warning=50,
        baselines={},
        max_workers=1,
        **limits,
    )


def test_a_cold_worker_build_is_not_counted_as_a_kill(tmp_path):
    result = _mutate_with(tmp_path, COLD_BUILD)

    assert result.statuses == {site.mutation_id: "survived" for site in result.sites}


def test_a_hanging_mutant_is_killed_by_the_baseline_timeout(tmp_path):
    hang = COLD_BUILD + "; if grep -q 'return 0' demo.py; then sleep 30; fi"
    started = time.monotonic()
    result = _mutate_with(tmp_path, hang)

    assert set(result.statuses.values()) == {"killed"}
    assert time.monotonic() - started < 15


def test_tests_that_fail_unmutated_in_a_worker_stop_the_run(tmp_path):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "python").touch()
    result = _mutate_with(tmp_path, "test -f .venv/python")

    assert result.baseline_failed
    assert "worker" in result.baseline_message
    assert not snapshot_path(tmp_path, "demo").exists()


def test_a_hanging_baseline_stops_at_the_baseline_timeout(tmp_path):
    started = time.monotonic()
    result = _mutate_with(tmp_path, "sleep 30", baseline_timeout=1)

    assert result.baseline_failed
    assert "Baseline timed out after 1 s for demo.py: sleep 30" in result.baseline_message
    assert time.monotonic() - started < 15


def test_a_hanging_control_run_stops_at_the_baseline_timeout(tmp_path):
    hang_in_worker = 'case "$PWD" in *mutation-workers*) sleep 30;; esac; true'
    started = time.monotonic()
    result = _mutate_with(tmp_path, hang_in_worker, baseline_timeout=1)

    assert result.baseline_failed
    assert "Unmutated tests timed out after 1 s in a mutation worker for demo.py" in result.baseline_message
    assert not snapshot_path(tmp_path, "demo").exists()
    assert time.monotonic() - started < 15


def test_verbose_names_the_mutant_timeout_and_where_it_comes_from(tmp_path, capsys):
    _mutate_with(tmp_path, "true", verbose=True)

    assert "Mutant timeout for demo.py: 2.0 s (baseline 0.0 s x 10, at least 2 s)" in capsys.readouterr().err


def test_ctrl_c_stops_the_worker_commands_and_removes_the_run_folder(tmp_path, ctrl_c_when):
    started = tmp_path / "started"
    late = tmp_path / "late"
    in_worker = f'case "$PWD" in *mutation-workers*) touch {started}; sleep 2; touch {late};; esac; true'
    ctrl_c_when(started)

    with pytest.raises(KeyboardInterrupt):
        _mutate_with(tmp_path, in_worker)

    # Within the 1 s grace, plus time to notice the stop and remove the folder.
    assert time.time() - started.stat().st_mtime < 2.0
    assert list((tmp_path / "target" / "mutation-workers").iterdir()) == []
    wait_until(started, 2.5)
    assert not late.exists()


def test_ctrl_c_while_the_workers_start_still_stops_them(tmp_path, monkeypatch):
    started = tmp_path / "started"
    late = tmp_path / "late"
    in_worker = f'case "$PWD" in *mutation-workers*) touch {started}; sleep 2; touch {late};; esac; true'

    class CtrlCDuringSubmit(ThreadPoolExecutor):
        def submit(self, *args, **kwargs):
            future = super().submit(*args, **kwargs)
            deadline = time.monotonic() + 20
            while not started.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            raise KeyboardInterrupt

    monkeypatch.setattr("mutator.workers.ThreadPoolExecutor", CtrlCDuringSubmit)

    with pytest.raises(KeyboardInterrupt):
        _mutate_with(tmp_path, in_worker)

    assert time.time() - started.stat().st_mtime < 2.0
    wait_until(started, 2.5)
    assert not late.exists()


def test_the_project_digest_is_none_when_a_listed_path_cannot_be_read(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    readable = project_files_digest(tmp_path)
    assert readable is not None
    # mutator's own output is no test input.
    for own in (".metrics/mutate/demo.edn", "target/mutation-workers/run-1/worker-0/demo.py", "target/mutator-backup/demo.py"):
        (tmp_path / own).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / own).write_text("{}", encoding="utf-8")
    assert project_files_digest(tmp_path) == readable
    # A folder whose name only starts the same is.
    (tmp_path / "target" / "mutation-workers-old").mkdir()
    (tmp_path / "target" / "mutation-workers-old" / "x").write_text("x", encoding="utf-8")
    assert project_files_digest(tmp_path) not in (None, readable)
    (tmp_path / "target" / "mutation-workers-old" / "x").unlink()
    assert project_files_digest(tmp_path) == readable
    secret = tmp_path / "secret.txt"
    secret.write_text("x", encoding="utf-8")
    secret.chmod(0)
    try:
        assert project_files_digest(tmp_path) is None
    finally:
        secret.chmod(0o600)
    assert project_files_digest(tmp_path / "missing") is None


def test_the_project_digest_of_a_subfolder_covers_only_that_folder(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    app = tmp_path / "app"
    app.mkdir()
    (app / "demo.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (tmp_path / "other.txt").write_text("a", encoding="utf-8")
    before = project_files_digest(app)
    assert before is not None
    (tmp_path / "other.txt").write_text("b", encoding="utf-8")
    assert project_files_digest(app) == before
    (app / ".metrics").mkdir()
    (app / ".metrics" / "crap.edn").write_text("{}", encoding="utf-8")
    assert project_files_digest(app) == before
    (app / "test_demo.py").write_text("", encoding="utf-8")
    assert project_files_digest(app) != before


def test_a_listed_symlink_counts_by_its_target_name(tmp_path):
    # bujo tracks .claude/skills/verify-bujo, a symlink to a folder.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "skills" / "verify").mkdir(parents=True)
    (tmp_path / "skills" / "verify" / "SKILL.md").write_text("a", encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "verify").symlink_to("../skills/verify")
    (tmp_path / "gone").symlink_to("nowhere")
    linked = project_files_digest(tmp_path)
    assert linked is not None
    assert project_files_digest(tmp_path) == linked
    (tmp_path / "gone").unlink()
    (tmp_path / "gone").symlink_to("elsewhere")
    assert project_files_digest(tmp_path) not in (None, linked)


def test_a_listed_file_counts_by_its_execute_bit_too(tmp_path):
    # Git records 100755 apart from 100644, and a runner can pick tests by it.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    script = tmp_path / "tests" / "check_f"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    runnable = project_files_digest(tmp_path)
    script.chmod(0o644)
    assert project_files_digest(tmp_path) not in (None, runnable)
    script.chmod(0o755)
    assert project_files_digest(tmp_path) == runnable


def test_a_listed_path_that_is_no_regular_file_keeps_nothing_and_does_not_block(tmp_path):
    # Opening a FIFO waits for a writer, so it must not be opened.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "data").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "data"], cwd=tmp_path, check=True)
    (tmp_path / "data").unlink()
    os.mkfifo(tmp_path / "data")
    done = []
    worker = threading.Thread(target=lambda: done.append(project_files_digest(tmp_path)), daemon=True)
    worker.start()
    worker.join(5)
    assert done == [None]
