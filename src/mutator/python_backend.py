"""Run mutmut in a disposable project copy and translate its results."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import sys
from pathlib import Path

from mutator.engine import _forms, project_files_digest
from mutator.functions import _owner, form_id, is_private, mutation_id, project_functions
from mutator.metrics import write_results
from mutator.model import Site
from mutator.report import format_results, format_scan, format_site_log
from mutator.runner import CommandRunner, test_plan
from mutator.workers import _make_temps, _remove_temp, create_workers, delete_tree, new_run_dir

BACKEND = "mutmut==3.8.0"


def _command(options, root: Path, files: list[Path]) -> tuple[str, list[str]]:
    if options.since_last_run:
        raise ValueError("mutmut starts a fresh run; --since-last-run requires --python-backend native")
    if options.coverage_command or options.use_existing_coverage:
        raise ValueError("mutmut collects its own test associations; external coverage requires the native backend")
    plans = [test_plan(root, path, "python", options.test_command) for path in files]
    if any(cwd != root for _, cwd in plans):
        raise ValueError("mutmut needs one Python project; set --root to its test configuration directory")
    command = plans[0][0]
    args = shlex.split(command) if isinstance(command, str) else command
    if any(plan[0] != command for plan in plans):
        raise ValueError("mutmut needs one pytest command for all selected Python files")
    if len(args) < 3 or args[1:3] != ["-m", "pytest"]:
        raise ValueError("mutmut requires a Python -m pytest command, without shell wrappers")
    if not hasattr(os, "fork"):
        raise ValueError("mutmut requires fork support; use Linux, macOS, or WSL")
    if any(word in {";", "&&", "||", "|", ">", "<"} for word in args):
        raise ValueError("mutmut requires a Python -m pytest command, without shell operators")
    return args[0], args[3:]


def _sites(root: Path, file: str, rows: list[dict]) -> tuple[list[Site], dict[str, str]]:
    path = root / file
    source = path.read_text(encoding="utf-8")
    data = source.encode("utf-8")
    functions = project_functions(source, path, root)
    sites = []
    statuses = {}
    for row in rows:
        start, end = row["start"], row["end"]
        if data[start:end].decode("utf-8") != row["original"]:
            raise ValueError(f"{file}: source changed during the mutmut run")
        owner = _owner(functions, row["line"], start)
        if owner is None:
            raise ValueError(f"{file}:{row['line']}: cannot map mutmut result to a crapper function")
        private = is_private("python", owner.name, source, owner.start_line, owner.end_line)
        form = form_id(owner.name, private)
        identity = json.loads(mutation_id(file, owner.namespace, form, start, end, row["original"], row["mutant"]))
        identity.append(BACKEND)
        key = json.dumps(identity, separators=(",", ":"))
        site = Site(file, owner.namespace, form, owner.name, row["line"], start, end,
                    row["original"], row["mutant"], "mutmut", key)
        if key in statuses:
            if statuses[key] != row["status"]:
                raise ValueError(f"{file}: identical mutations received conflicting results")
            continue
        sites.append(site)
        statuses[key] = row["status"]
    return sites, statuses


def _local_links(root: Path, worker: Path) -> None:
    """Keep links into this project inside the disposable copy."""
    for directory, names, files in os.walk(worker, followlinks=False):
        for name in names + files:
            link = Path(directory) / name
            if not link.is_symlink():
                continue
            original = root / link.relative_to(worker)
            if not original.is_symlink():
                # Native workers share nested caches and build directories.
                # Omit these instead of making a link point back to itself.
                link.unlink()
                continue
            target = original.resolve()
            if target.is_relative_to(root):
                link.unlink()
                link.symlink_to(os.path.relpath(worker / target.relative_to(root), link.parent))


def _record(options, root: Path, files: list[Path], report: dict, context: str) -> int:
    prepared = []
    for path in files:
        file = path.relative_to(root).as_posix()
        sites, statuses = _sites(root, file, report[file])
        covered = {site.mutation_id: statuses[site.mutation_id] != "uncovered" for site in sites}
        if options.scan:
            print(format_scan(file, sites, set(), None), end="")
            continue
        outcomes = {key: status for key, status in statuses.items() if status != "uncovered"}
        forms = _forms(path.read_text(encoding="utf-8"), path, root, file, sites, covered,
                       outcomes, options.lines, context)
        prepared.append((file, sites, statuses, forms, outcomes))
    all_forms = []
    for file, sites, statuses, forms, outcomes in prepared:
        written = write_results(root, file, forms, outcomes)
        print(format_site_log(sites, statuses), end="")
        all_forms.extend(forms)
        for path in written:
            print(f"Wrote {path}", file=sys.stderr)
    if all_forms:
        print(format_results(all_forms), end="")
    return 3 if any(form.survived for form in all_forms) else 0


def run_python(options, root: Path, files: list[Path]) -> int:
    base = None
    temps = []
    try:
        interpreter, pytest_args = _command(options, root, files)
        before = {path: path.read_bytes() for path in files}
        files_digest = project_files_digest(root)
        base = new_run_dir(root)
        for path in files:
            directories = create_workers(base, root, path.relative_to(root).as_posix(), before[path], 1)
        worker = directories[0]
        _local_links(root, worker)
        _make_temps(directories, temps)
        request = {
            "files": [path.relative_to(root).as_posix() for path in files],
            "lines": sorted(options.lines) if options.lines is not None else None,
            "pytest_args": pytest_args,
            "workers": min(options.max_workers or (os.cpu_count() or 1), os.cpu_count() or 1),
            "timeout_factor": options.timeout_factor,
            "scan": options.scan,
            "no_coverage": options.no_coverage,
        }
        request_path = base / "request.json"
        report_path = base / "report.json"
        request_path.write_text(json.dumps(request), encoding="utf-8")
        helper = Path(__file__).with_name("mutmut_bridge.py")
        runner = CommandRunner(verbose=options.verbose, memory_limit=options.memory_limit)
        # Bounds setup as well as the whole run. Individual mutants have mutmut's
        # own timeout. A whole-run timeout is an error, never a mutation kill.
        result = runner.run([interpreter, "-P", str(helper), str(request_path), str(report_path)],
                            worker, options.baseline_timeout)
        if options.verbose or result.code or result.timed_out:
            print(result.output, file=sys.stderr, end="")
        if result.code or result.timed_out:
            raise ValueError("mutmut did not complete; no Python snapshots were written")
        if any(path.read_bytes() != original for path, original in before.items()):
            raise ValueError("Python source changed during the run; no snapshots were written")
        report = json.loads(report_path.read_text(encoding="utf-8"))
        if set(report) != set(request["files"]):
            raise ValueError("mutmut returned an incomplete file report")
        context = hashlib.sha256(json.dumps([BACKEND, files_digest, request]).encode()).hexdigest()
        return _record(options, root, files, report, context)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        print(f"Python backend: {exc}", file=sys.stderr)
        return 2
    finally:
        if base is not None:
            delete_tree(base)
        for folder in temps:
            _remove_temp(folder)
