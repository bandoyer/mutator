"""Apply each selected mutant, run tests, and record the snapshot."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

from mutator.crapper_link import ensure_crapper
from mutator.functions import form_digests, form_spans, sites_in_file
from mutator.metrics import load_history, source_key, write_results
from mutator.model import FormResult, RunResult, Site
from mutator.report import format_scan
from mutator.runner import Command, CommandResult, CommandRunner, display_command, test_plan
from mutator.workers import WorkerFailed, run_mutants

# The unmutated runs can't use the mutant timeout: the baseline sets it, and a
# worker's control run is a cold build that can be much slower than the baseline.
BASELINE_TIMEOUT = 600.0

# mutator's own output, which changes on every run and is no test input.
_OWN_OUTPUT = (".metrics/", "target/mutation-workers/", "target/mutator-backup/")


def check_backups(root: Path) -> list[tuple[Path, Path]]:
    """Delete each backup an older, interrupted run left that equals its source.

    Return the (backup, source) pairs that differ. Only the user knows which
    file holds the edit to keep, so neither is written.
    """

    base = root / "target" / "mutator-backup"
    differing: list[tuple[Path, Path]] = []
    if not base.is_dir():
        return differing
    for backup in sorted(path for path in base.rglob("*") if path.is_file()):
        source = root / backup.relative_to(base)
        if source.is_file() and source.read_bytes() == backup.read_bytes():
            backup.unlink()
        else:
            differing.append((backup, source))
    return differing


def _drop_bytecode(path: Path) -> None:
    """A same-second .pyc would hide the mutant from Python."""

    cache = path.parent / "__pycache__"
    if not cache.is_dir():
        return
    for item in cache.glob(path.stem + ".*.pyc"):
        item.unlink(missing_ok=True)


def _backup(root: Path, path: Path, original: bytes) -> Path:
    relative = path.resolve().relative_to(root.resolve())
    backup = root / "target" / "mutator-backup" / relative
    backup.parent.mkdir(parents=True, exist_ok=True)
    if not backup.exists():
        backup.write_bytes(original)
    return backup


def _covered(site: Site, lines: set[int] | None, ignore_coverage: bool) -> bool:
    if ignore_coverage:
        return True
    if lines is None:
        return False
    return site.line in lines


def project_files_digest(root: Path) -> str | None:
    """A digest of every file git lists under `root`, tracked or untracked but not ignored.

    mutator's own output is left out. A listed file that is gone counts as
    missing. A symlink counts by its target's name, as git stores it. None
    when git can't list the files, or a listed path can't be read, such as a
    submodule: then no kill can be kept.
    """

    try:
        listed = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if listed.returncode != 0:
        return None
    digest = hashlib.sha256()
    for name in sorted(set(listed.stdout.split(b"\0")) - {b""}):
        relative = os.fsdecode(name)
        if relative.startswith(_OWN_OUTPUT):
            continue
        path = root / relative
        try:
            content = _listed_content(path)
        except OSError:
            return None
        digest.update(name + b"\0" + content)
    return digest.hexdigest()


def _listed_content(path: Path) -> bytes:
    if path.is_symlink():
        return b"link:" + os.fsencode(os.readlink(path))
    if not path.exists():
        return b"missing"
    with open(path, "rb") as stream:
        return hashlib.file_digest(stream, "sha256").digest()


def _test_context(files_digest: str | None, command: Command, cwd: Path, timeout_factor: float) -> str | None:
    """What a kill depends on besides its own function. None when it can't be known."""

    if files_digest is None:
        return None
    identity = repr((files_digest, _command_key(command), str(cwd), timeout_factor))
    return hashlib.sha256(identity.encode()).hexdigest()


def _unchanged(history, key: tuple[str, str], digests, context: str | None) -> bool:
    prior = history.forms.get(key)
    if prior is None or context is None:
        return False
    return prior.digest == digests.get(key) and prior.context == context


def _select(
    sites: list[Site],
    covered: dict[str, bool],
    history,
    digests: dict[tuple[str, str], str],
    mutate_all: bool,
    lines: set[int] | None,
    context: str | None,
) -> list[Site]:
    chosen = []
    for site in sites:
        if not covered[site.mutation_id]:
            continue
        if lines is not None and site.line not in lines:
            continue
        unchanged = _unchanged(history, (site.namespace, site.form_id), digests, context)
        if mutate_all or not unchanged:
            chosen.append(site)
            continue
        if history.outcomes.get(site.mutation_id) == "killed":
            continue
        chosen.append(site)
    return chosen


def _carry_forward(
    sites: list[Site], covered: dict[str, bool], history, digests, context: str | None
) -> dict[str, str]:
    by_id = {site.mutation_id: site for site in sites}
    carried = {}
    for mutation, status in history.outcomes.items():
        site = by_id.get(mutation)
        if site is None or not covered[site.mutation_id]:
            continue
        if _unchanged(history, (site.namespace, site.form_id), digests, context):
            carried[mutation] = status
    return carried


def _forms(
    source: str,
    path: Path,
    root: Path,
    file_key: str,
    sites: list[Site],
    covered: dict[str, bool],
    outcomes: dict[str, str],
    lines: set[int] | None,
    context: str | None,
) -> list[FormResult]:
    spans = form_spans(source, path, root)
    digests = form_digests(source, path, root)
    grouped: dict[tuple[str, str], list[Site]] = {}
    for site in sites:
        grouped.setdefault((site.namespace, site.form_id), []).append(site)
    forms = []
    for key, (start, end, name, private) in sorted(spans.items(), key=lambda item: item[1][0]):
        namespace, form = key
        owned = grouped.get(key, [])
        killed = survived = uncovered = 0
        for site in owned:
            if not covered[site.mutation_id]:
                uncovered += 1
                continue
            status = outcomes.get(site.mutation_id)
            if status == "killed":
                killed += 1
            elif status == "survived":
                survived += 1
            elif lines is None:
                survived += 1
        forms.append(
            FormResult(
                id=form,
                namespace=namespace,
                name=name,
                private=private,
                file=file_key,
                line=start,
                end_line=end,
                digest=digests[key],
                killed=killed,
                survived=survived,
                uncovered=uncovered,
                sites=len(owned),
                context=context,
            )
        )
    return forms


def _decode(path: Path) -> tuple[bytes, str] | None:
    original = path.read_bytes()
    try:
        return original, original.decode("utf-8")
    except UnicodeError:
        return None


def _coverage_map(
    found: list[Site], covered_lines: set[int] | None, ignore_coverage: bool
) -> dict[str, bool]:
    return {site.mutation_id: _covered(site, covered_lines, ignore_coverage) for site in found}


def _warn_uncovered(file_key: str, found, covered_lines, ignore_coverage: bool) -> None:
    if covered_lines is not None or ignore_coverage or not found:
        return
    print(
        f"No coverage data for {file_key}; its sites are uncovered. "
        "Pass --no-coverage to run them anyway.",
        file=sys.stderr,
    )


def _warn_many(file_key: str, selected, mutation_warning: int) -> None:
    if len(selected) > mutation_warning:
        print(
            f"WARNING: {len(selected)} covered mutations selected in {file_key}.",
            file=sys.stderr,
        )


def _command_key(command: Command) -> tuple[str, ...]:
    """A hashable identity. A list cannot be a dict key."""

    if isinstance(command, str):
        return ("sh", command)
    return ("argv", *command)


def _remember_baseline(baselines, cache_key, runner, command: Command, cwd: Path, limit: float) -> None:
    if cache_key not in baselines:
        baselines[cache_key] = runner.run(command, cwd, limit)


def _how(result: CommandResult, limit: float) -> str:
    if result.timed_out:
        return f"timed out after {limit:g} s"
    return "failed"


def _tail(output: str) -> str:
    return "\n".join(output.splitlines()[-20:])


def _baseline_failure(
    file_key: str, command: Command, tail: str, failed: str
) -> RunResult:
    message = f"{failed} for {file_key}: {display_command(command)}"
    if tail:
        message = f"{message}\n{tail}"
    return RunResult(path=file_key, forms=[], written=[], baseline_failed=True, baseline_message=message)


def _apply_selected(
    path,
    root,
    original,
    selected,
    runner,
    command,
    cwd,
    timeout_factor,
    file_key,
    baselines,
    outcomes,
    max_workers,
    baseline_timeout,
) -> RunResult | None:
    # A kept kill is reported only after the tests pass on the code as it is now.
    if not selected and not outcomes:
        return None
    cache_key = (_command_key(command), str(cwd))
    _remember_baseline(baselines, cache_key, runner, command, cwd, baseline_timeout)
    baseline = baselines[cache_key]
    if baseline.code != 0:
        failed = f"Baseline {_how(baseline, baseline_timeout)}"
        return _baseline_failure(file_key, command, _tail(baseline.output), failed)
    if not selected:
        return None
    seconds = baseline.seconds
    timeout = max(2.0, seconds * timeout_factor)
    if runner.verbose:
        rule = f"baseline {seconds:.1f} s x {timeout_factor:g}, at least 2 s"
        print(f"Mutant timeout for {file_key}: {timeout:.1f} s ({rule})", file=sys.stderr)
    try:
        run_mutants(
            root,
            path,
            original,
            selected,
            max_workers,
            runner,
            command,
            cwd,
            timeout,
            baseline_timeout,
            file_key,
            outcomes,
        )
    except WorkerFailed as failure:
        failed = f"Unmutated tests {_how(failure.result, baseline_timeout)} in a mutation worker"
        return _baseline_failure(file_key, command, _tail(failure.result.output), failed)
    return None


def _statuses(found: list[Site], covered: dict[str, bool], outcomes: dict[str, str]) -> dict[str, str]:
    statuses = {}
    for site in found:
        if not covered[site.mutation_id]:
            statuses[site.mutation_id] = "uncovered"
        elif site.mutation_id in outcomes:
            statuses[site.mutation_id] = outcomes[site.mutation_id]
    return statuses


def mutate_file(
    path: Path,
    root: Path,
    *,
    runner: CommandRunner,
    covered_lines: set[int] | None,
    ignore_coverage: bool,
    mutate_all: bool,
    lines: set[int] | None,
    test_command: str | None,
    timeout_factor: float,
    mutation_warning: int,
    baselines: dict[tuple[tuple[str, ...], str], CommandResult],
    max_workers: int | None = None,
    baseline_timeout: float = BASELINE_TIMEOUT,
    files_digest: str | None = None,
) -> RunResult:
    """Mutate one file and write its namespaces into `.metrics/mutate`.

    `files_digest` is `project_files_digest(root)`, taken once per run. A kill
    from the snapshot is kept only when it and the test command are unchanged.
    """

    root = root.resolve()
    path = path.resolve()
    crapper = ensure_crapper()
    language = crapper.discover.language_of(path)
    file_key = source_key(path, root)
    if language is None:
        return RunResult(path=file_key, forms=[], written=[], skipped="unsupported file")
    decoded = _decode(path)
    if decoded is None:
        return RunResult(path=file_key, forms=[], written=[], skipped="file is not UTF-8")
    original, source = decoded
    found = sites_in_file(source, path, root, file_key)
    covered = _coverage_map(found, covered_lines, ignore_coverage)
    _warn_uncovered(file_key, found, covered_lines, ignore_coverage)
    history = load_history(root, file_key)
    digests = form_digests(source, path, root)
    command, cwd = test_plan(root, path, language, test_command)
    context = _test_context(files_digest, command, cwd, timeout_factor)
    selected = _select(found, covered, history, digests, mutate_all, lines, context)
    outcomes = _carry_forward(found, covered, history, digests, context)
    _warn_many(file_key, selected, mutation_warning)
    failure = _apply_selected(
        path,
        root,
        original,
        selected,
        runner,
        command,
        cwd,
        timeout_factor,
        file_key,
        baselines,
        outcomes,
        max_workers,
        baseline_timeout,
    )
    if failure is not None:
        return failure
    forms = _forms(source, path, root, file_key, found, covered, outcomes, lines, context)
    written = write_results(root, file_key, forms, outcomes)
    return RunResult(
        path=file_key,
        forms=forms,
        written=[item.as_posix() for item in written],
        sites=found,
        statuses=_statuses(found, covered, outcomes),
    )


def scan_file(
    path: Path,
    root: Path,
    *,
    covered_lines: set[int] | None,
    ignore_coverage: bool,
    lines: set[int] | None,
) -> str:
    """Inventory mutation sites without running tests or writing metrics."""

    root = root.resolve()
    path = path.resolve()
    file_key = source_key(path, root)
    source = path.read_text(encoding="utf-8")
    found = sites_in_file(source, path, root, file_key)
    if lines is not None:
        found = [site for site in found if site.line in lines]
    covered = None
    if not ignore_coverage and covered_lines is not None:
        covered = {site.mutation_id: site.line in covered_lines for site in found}
    history = load_history(root, file_key)
    digests = form_digests(source, path, root)
    changed = set()
    if history.forms:
        changed = {
            key
            for key, digest in digests.items()
            if key not in history.forms or history.forms[key].digest != digest
        }
    return format_scan(file_key, found, changed, covered)
