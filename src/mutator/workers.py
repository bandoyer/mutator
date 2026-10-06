"""Run mutants in private copies of the project, one per worker.

Each worker is a directory under target/mutation-workers. Project files are
copied in, so workers can mutate the file under test, and tests can write
files, at the same time without touching the original tree or each other.
Only node_modules, a shared dependency cache, and the project's own symlinks
are linked. The worker is removed when the file's mutants finish.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue
from threading import Lock

from mutator.model import Site
from mutator.runner import RUN_MARKER, Command, CommandResult, worker_temp_link
from mutator.sites import apply_site

# Build output and the worker tree itself must not be shared. A link to
# ``target`` would point a worker at the directory that contains the worker.
SKIP_LINK = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".venv",
        "venv",
        "target",
        "dist",
        "build",
        "out",
        "coverage",
        ".metrics",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".clj-kondo",
        ".gradle",
    }
)

class WorkerFailed(Exception):
    """The unmutated tests failed or timed out in a worker, so a failing mutant there proves nothing."""

    def __init__(self, result: CommandResult):
        super().__init__(result.output)
        self.result = result


def worker_count(site_count: int, requested: int | None) -> int:
    """The smaller of the sites, the cores, and an explicit --max-workers."""

    processors = os.cpu_count() or 1
    limit = processors if requested is None else requested
    return max(1, min(site_count, processors, limit))


def new_run_dir(root: Path) -> Path:
    directory = root / "target" / "mutation-workers" / f"run-{uuid.uuid4()}"
    directory.mkdir(parents=True)
    (directory / RUN_MARKER).write_text("mutator's workers for one run (issue #57).\n", encoding="utf-8")
    return directory


def _holds_workers(folder: Path) -> bool:
    return (folder.name == "mutation-workers" and folder.parent.name == "target") or (folder / RUN_MARKER).is_file()


def inside_workers(path: Path) -> bool:
    """Whether `path` is in target/mutation-workers or in a run folder new_run_dir made.

    A mutant exists only there, so a run rooted there would test the mutant's
    own copy and could start the next such run (issue #57). The folder names
    also catch workers that an older mutator made without the marker.
    """

    return any(_holds_workers(folder) for folder in (path, *path.parents))


def symlink(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target.resolve())


def delete_tree(path: Path) -> None:
    """Remove a worker tree without following a link into the real project."""

    if path.is_symlink():
        path.unlink()
        return
    if not path.exists():
        return
    if path.is_dir():
        for child in list(path.iterdir()):
            delete_tree(child)
        path.rmdir()
        return
    path.unlink()


# A nested .git can name the project's repository, so no worker gets one.
VCS = frozenset({".git", ".hg", ".svn"})
# A __pycache__ holds bytecode of the project's own sources. Python runs a .pyc
# in place of a source whose mtime, in whole seconds, and size it matches, and
# an unchecked-hash .pyc whatever the source says. A worker that saw the
# project's would run the original in place of a same-size mutant (issue #82).
LEFT_OUT = VCS | {"__pycache__"}
# Dependencies and build output, below the root, are shared as main shared them:
# copying a nested .venv or target into every worker has no bound.
SHARED = (SKIP_LINK - LEFT_OUT) | {"node_modules"}


def _copy_entry(destination: Path, source: Path) -> None:
    """Copy `source`, so a test that writes to it writes only in the worker (issue #8).

    Version control folders and __pycache__ are left out. A project's own symlinks and the
    SHARED folders stay links. A folder that holds mutator's workers stays
    empty, or a worker would copy itself. A FIFO, socket, or device is left
    out: reading one to copy it could block.
    """

    if source.name in LEFT_OUT:
        return
    if source.is_symlink() or source.name in SHARED:
        symlink(destination, source)
    elif source.is_dir():
        destination.mkdir()
        if _holds_workers(source):
            return
        for child in source.iterdir():
            _copy_entry(destination / child.name, child)
    elif source.is_file():
        shutil.copy2(source, destination)


def _copy_children(worker_dir: Path, real_dir: Path) -> None:
    # A linked node_modules on the source path would take the mutant to the original.
    if worker_dir.is_symlink():
        worker_dir.unlink()
    worker_dir.mkdir(exist_ok=True)
    for child in real_dir.iterdir():
        if child.name in SKIP_LINK:
            continue
        destination = worker_dir / child.name
        if destination.exists() or destination.is_symlink():
            continue
        _copy_entry(destination, child)


def _overlay(worker: Path, root: Path, relative: str, original: bytes) -> None:
    """Copy the project, and every folder on the source path, even one named in SKIP_LINK."""

    segments = relative.split("/")
    for index in range(len(segments)):
        rel_dir = Path(*segments[:index])
        _copy_children(worker / rel_dir, root / rel_dir)
    # A new file, as the copy keeps a read-only source's mode.
    (worker / relative).unlink()
    (worker / relative).write_bytes(original)


def create_workers(
    base: Path, root: Path, relative: str, original: bytes, count: int
) -> list[Path]:
    created = []
    for index in range(count):
        worker = base / f"worker-{index}"
        _overlay(worker, root, relative, original)
        created.append(worker)
    return created


def _drop_bytecode(path: Path) -> None:
    """Remove `path`'s .pyc files from its folder's __pycache__ in the worker.

    A test command that writes bytecode leaves the .pyc of the worker's
    unmutated run, or of its last mutant, beside the source. A same-size
    mutant written in the same second would load it in place of its own code
    (issue #82).
    """

    own = path.stem + "."
    try:
        entries = os.scandir(path.parent / "__pycache__")
    except OSError:
        return
    # A literal match: a name such as demo[1] is no glob pattern. The listing
    # streams, so a large __pycache__ is never held in memory at once.
    with entries:
        for entry in entries:
            if entry.name.startswith(own) and entry.name.endswith(".pyc"):
                Path(entry.path).unlink(missing_ok=True)


def mapped_cwd(worker: Path, root: Path, cwd: Path) -> Path:
    try:
        relative = cwd.resolve().relative_to(root.resolve())
    except ValueError:
        return worker
    return (worker / relative).resolve()


def _execute(
    directory: Path,
    site: Site,
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    file_key: str,
) -> str:
    current = original[site.start : site.end].decode("utf-8")
    if current != site.original:
        print(
            f"Skipped {file_key}:{site.line} {site.description}; source bytes moved",
            file=sys.stderr,
        )
        return "survived"
    destination = directory / relative
    mutated = apply_site(original.decode("utf-8"), site.start, site.end, site.mutant)
    destination.write_text(mutated, encoding="utf-8")
    _drop_bytecode(destination)
    try:
        result = runner.run(command, mapped_cwd(directory, root, cwd), timeout)
    finally:
        destination.write_bytes(original)
    if result.timed_out or result.code != 0:
        return "killed"
    return "survived"


def _drain(
    directory: Path,
    pending: Queue,
    lock: Lock,
    outcomes: dict[str, str],
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    baseline_timeout: float,
    file_key: str,
) -> None:
    # The worker shares no build output with the project. This unmutated run
    # builds it, so mutant runs start as warm as the baseline did and its
    # timeout is fair. It also proves the tests can pass in the worker at all.
    control = runner.run(command, mapped_cwd(directory, root, cwd), baseline_timeout)
    if control.code != 0:
        raise WorkerFailed(control)
    while True:
        try:
            site = pending.get_nowait()
        except Empty:
            return
        if runner.verbose:
            with lock:
                _report(file_key, site)
        status = _execute(
            directory,
            site,
            root,
            relative,
            original,
            runner,
            command,
            cwd,
            timeout,
            file_key,
        )
        with lock:
            outcomes[site.mutation_id] = status


def _report(file_key: str, site: Site) -> None:
    print(f"{file_key}:{site.line} {site.description}", file=sys.stderr)


def _run_all(
    directories: list[Path],
    sites: list[Site],
    outcomes: dict[str, str],
    root: Path,
    relative: str,
    original: bytes,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    baseline_timeout: float,
    file_key: str,
) -> None:
    pending: Queue = Queue()
    for site in sites:
        pending.put(site)
    lock = Lock()
    with ThreadPoolExecutor(max_workers=len(directories)) as pool:
        try:
            futures = [
                pool.submit(
                    _drain,
                    directory,
                    pending,
                    lock,
                    outcomes,
                    root,
                    relative,
                    original,
                    runner,
                    command,
                    cwd,
                    timeout,
                    baseline_timeout,
                    file_key,
                )
                for directory in directories
            ]
            for future in futures:
                future.result()
        except (KeyboardInterrupt, SystemExit):
            # Only the main thread sees Ctrl-C or SIGTERM. The workers' commands
            # must stop too, or leaving this block waits for each of them.
            runner.stop()
            raise


def _make_temps(directories: list[Path], temps: list[str]) -> None:
    """Give each worker a temp folder of its own (issue #54), in one private folder it adds to `temps`.

    The folders are short ones in mutator's own temp folder. One in the worker
    would be inside target/mutation-workers, where mutator refuses to run, and
    a Unix socket path in it could pass the 107-byte limit.
    """

    run_temp = Path(tempfile.mkdtemp(prefix="mutator-"))
    temps.append(str(run_temp))
    for directory in directories:
        own = run_temp / directory.name
        own.mkdir()
        worker_temp_link(directory).symlink_to(own)


def _remove_temp(folder: str) -> None:
    """Remove a run's temp folder, also folders its tests left unreadable or read-only.

    Folders are opened up first, never through a symlink, whose target can be a
    project folder. A test can even replace the run's folder with one: then
    only the link goes. What still can't be removed is named on stderr.
    """

    if os.path.islink(folder):
        os.unlink(folder)
        return
    with contextlib.suppress(OSError):
        os.chmod(folder, 0o700)
        for parent, names, _files in os.walk(folder):
            for name in names:
                path = os.path.join(parent, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o700)
    shutil.rmtree(folder, ignore_errors=True)
    if os.path.lexists(folder):
        print(f"mutator could not remove its temp folder {folder}", file=sys.stderr)


def run_mutants(
    root: Path,
    source: Path,
    original: bytes,
    sites: list[Site],
    max_workers: int | None,
    runner,
    command: Command,
    cwd: Path,
    timeout: float,
    baseline_timeout: float,
    file_key: str,
    outcomes: dict[str, str],
) -> None:
    """Mutate ``sites`` in parallel overlays of ``root``. The source stays put."""

    if not sites:
        return
    base = new_run_dir(root)
    temps: list[str] = []
    try:
        relative = source.resolve().relative_to(root.resolve()).as_posix()
        directories = create_workers(
            base, root, relative, original, worker_count(len(sites), max_workers)
        )
        _make_temps(directories, temps)
        _run_all(
            directories,
            sites,
            outcomes,
            root,
            relative,
            original,
            runner,
            command,
            cwd,
            timeout,
            baseline_timeout,
            file_key,
        )
    finally:
        for temp in temps:
            _remove_temp(temp)
        delete_tree(base)
