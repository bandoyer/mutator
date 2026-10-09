"""Private subprocess protocol for exactly mutmut 3.8.0.

This file runs with the project's Python, so it imports no mutator modules.
All reliance on mutmut's private generation and result APIs lives here.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import shutil
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path


def configure(request):
    import tomli_w

    if version("mutmut") != "3.8.0":
        raise ValueError("install mutmut==3.8.0 and tomli-w in the project's Python environment")
    path = Path("pyproject.toml")
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    if Path("pytest.ini").exists() or Path("tox.ini").exists() or (
        Path("setup.cfg").exists() and not data.get("tool", {}).get("pytest")
    ):
        raise ValueError("this mutmut adapter needs pytest configuration in pyproject.toml")
    pytest_config = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    roots = [Path(p) for p in pytest_config.get("pythonpath", [])]
    roots += [Path("src"), Path(".")]
    if any(p.is_absolute() or ".." in p.parts for p in roots):
        raise ValueError("mutmut needs Python import paths inside the project")
    prior = data.get("tool", {}).get("mutmut", {})
    unsupported = set(prior) - {"pytest_add_cli_args", "pytest_add_cli_args_test_selection"}
    if unsupported:
        raise ValueError(f"adapter cannot preserve these tool.mutmut settings: {', '.join(sorted(unsupported))}")
    data.setdefault("tool", {})["mutmut"] = {
        "source_paths": request["files"],
        "also_copy": [p.name for p in Path(".").iterdir()
                      if p.name not in {"mutants", "target", ".git", ".metrics", ".venv", "venv"}],
        "pytest_add_cli_args": prior.get("pytest_add_cli_args", []) + request["pytest_args"],
        "pytest_add_cli_args_test_selection": prior.get("pytest_add_cli_args_test_selection", []),
        "timeout_multiplier": request["timeout_factor"],
        "use_setproctitle": False,
    }
    path.write_text(tomli_w.dumps(data), encoding="utf-8")
    return roots


def install_import_names(api, roots):
    from mutmut.utils.format_utils import get_mutant_name

    def name(path, function):
        for root in roots:
            if Path(path).is_relative_to(root):
                return get_mutant_name(Path(path).relative_to(root), function)
        raise ValueError(f"no import path for {path}")

    # Metadata must name the same module pytest imports. Upstream only strips
    # 'src/', but projects also use pytest's pythonpath for roots like 'lib/'.
    api.get_mutant_name = name


def preserve_links(api):
    from mutmut.configuration import config

    copied = set()

    def copy_files():
        for source in config().also_copy:
            source = Path(source)
            # Generation runs once for site selection and again in api.run.
            # Copy each tree once; copytree cannot overlay existing symlinks.
            if source in copied:
                continue
            destination = Path("mutants") / source
            if source.is_symlink():
                destination.unlink(missing_ok=True)
                destination.symlink_to(os.readlink(source))
            elif source.is_dir():
                shutil.copytree(source, destination, symlinks=True, dirs_exist_ok=True)
            elif source.is_file():
                shutil.copy2(source, destination)
            copied.add(source)

    api.copy_also_copy_files = copy_files


def functions_by_name(module):
    import libcst as cst

    class Names(cst.CSTVisitor):
        def __init__(self):
            self.stack = []
            self.functions = {}

        def visit_ClassDef(self, node):
            self.stack.append(node.name.value)

        def leave_ClassDef(self, node):
            self.stack.pop()

        def visit_FunctionDef(self, node):
            self.functions[tuple(self.stack + [node.name.value])] = node
            self.stack.append(node.name.value)

        def leave_FunctionDef(self, node):
            self.stack.pop()

    visitor = Names()
    module.visit(visitor)
    return visitor.functions


def replacement(module, node, generated):
    generated = generated.with_changes(decorators=node.decorators, leading_lines=node.leading_lines)
    return module.deep_replace(node, generated).code


def difference(original: str, mutant: str) -> dict:
    start = 0
    limit = min(len(original), len(mutant))
    while start < limit and original[start] == mutant[start]:
        start += 1
    end, changed_end = len(original), len(mutant)
    while end > start and changed_end > start and original[end - 1] == mutant[changed_end - 1]:
        end -= 1
        changed_end -= 1
    if start == end == changed_end:
        raise ValueError("mutmut generated an unchanged mutation")
    return {"line": original.count("\n", 0, start) + 1,
            "start": len(original[:start].encode()), "end": len(original[:end].encode()),
            "original": original[start:end], "mutant": mutant[start:changed_end]}


def collect_sites(files):
    import libcst as cst
    from mutmut.mutation.data import SourceFileMutationData
    from mutmut.mutation.diff_apply import read_functions_from_index
    from mutmut.utils.format_utils import orig_function_and_class_names_from_key

    report = {}
    for file in files:
        source = Path(file).read_text(encoding="utf-8")
        module = cst.parse_module(source)
        nodes = functions_by_name(module)
        meta = SourceFileMutationData(path=file)
        meta.load()
        rows = []
        for key in meta.exit_code_by_key:
            function, cls = orig_function_and_class_names_from_key(key)
            node = nodes[(cls, function) if cls else (function,)]
            original, mutated = read_functions_from_index(key, file)
            if replacement(module, node, original) != source:
                raise ValueError(f"{key}: generated original differs from the source")
            row = difference(source, replacement(module, node, mutated))
            rows.append(dict(row, key=key, status="not checked"))
        report[file] = rows
    return report


def outcomes(report):
    from mutmut.mutation.data import SourceFileMutationData

    statuses = {0: "survived", 1: "killed", 5: "uncovered", 33: "uncovered",
                36: "killed", -24: "killed", 24: "killed", 152: "killed", 255: "killed"}
    for file, rows in report.items():
        meta = SourceFileMutationData(path=file)
        meta.load()
        for row in rows:
            code = meta.exit_code_by_key[row["key"]]
            if code not in statuses:
                raise ValueError(f"{row['key']}: incomplete or erroneous result {code!r}")
            row["status"] = statuses[code]


def run(request):
    roots = configure(request)
    import mutmut.__main__ as api
    from mutmut.state import state

    multiprocessing.set_start_method("fork", force=True)
    install_import_names(api, roots)
    preserve_links(api)
    Path("mutants").mkdir()
    api.copy_src_dir()
    api.copy_also_copy_files()
    api.setup_source_paths()
    if request["lines"] is not None:
        state()._covered_lines = {str((Path("mutants") / file).absolute()): set(request["lines"])
                                 for file in request["files"]}
    api.create_mutants(request["workers"])
    report = collect_sites(request["files"])
    if request["lines"] is not None:
        for file in report:
            report[file] = [row for row in report[file] if row["line"] in request["lines"]]
    names = [row["key"] for rows in report.values() for row in rows]
    if request["scan"]:
        return report
    if not names:
        from mutmut.runners.harness import PytestRunner

        if PytestRunner().run_tests(mutant_name=None, tests=[]):
            raise ValueError("clean tests failed, even though no mutation was selected")
        return report
    if request["no_coverage"]:
        collect = api.collect_or_load_stats

        def all_unassociated(*args, **kwargs):
            collect(*args, **kwargs)
            for key in names:
                function = api.mangled_name_from_mutant_name(key)
                if not state().tests_by_mangled_function_name.get(function):
                    state().tests_by_mangled_function_name[function] = set(state().duration_by_test)

        api.collect_or_load_stats = all_unassociated
    api.run.callback(tuple(names), max_children=request["workers"])
    outcomes(report)
    return report


if __name__ == "__main__":
    try:
        request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
        result = run(request)
        Path(sys.argv[2]).write_text(json.dumps(result), encoding="utf-8")
    except ImportError as exc:
        print(f"mutmut backend: {exc}. Install mutmut==3.8.0 and tomli-w in {sys.executable}", file=sys.stderr)
        sys.exit(2)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"mutmut backend: {exc}", file=sys.stderr)
        sys.exit(2)
