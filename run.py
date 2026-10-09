import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Set

from .emcapsulation import (
    BOUNDARIES_FILENAME,
    BoundaryError,
    check_boundaries,
    find_boundary_files,
    format_boundary_report,
    load_boundaries,
)
from .get_python_stuff import classify_imports, drop_imports, probe_environment
from .plot import (
    boundary_graph_to_dot,
    build_boundary_graph,
    build_import_graph,
    graph_to_dot,
)
from .symbols import FileReport, analyze_project, format_text_report

_CONFIG_META = {"help", "config", "dump_config"}
_CONFIG_PATH_KEYS = {"path", "output", "python", "boundaries"}


def _config_value(key: str, value, action: argparse.Action, fail) -> object:
    if action.nargs == 0:
        if not isinstance(value, bool):
            fail(f"{key!r} must be true or false")
        return value
    if isinstance(action.default, list):
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            fail(f"{key!r} must be a string or a list of strings")
        return list(value)
    if value is None:
        return None
    if action.type is int:
        if isinstance(value, bool) or not isinstance(value, int):
            fail(f"{key!r} must be an integer")
    elif not isinstance(value, str):
        fail(f"{key!r} must be a string")
    if action.choices and value not in action.choices:
        fail(f"{key!r} must be one of: {', '.join(action.choices)}")
    return value


def load_config(path: str, parser: argparse.ArgumentParser) -> dict:

    def fail(msg: str):
        parser.error(f"config {path}: {msg}")

    cfg_path = Path(path)
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except OSError as exc:
        fail(exc.strerror or str(exc))
    except json.JSONDecodeError as exc:
        fail(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if not isinstance(data, dict):
        fail("must contain a JSON object")

    actions = {a.dest: a for a in parser._actions if a.dest not in _CONFIG_META}
    values = {}
    for key, value in data.items():
        if key.startswith("_"):
            continue
        dest = key.replace("-", "_")
        if dest not in actions:
            fail(f"unknown key {key!r}. Valid keys: {', '.join(sorted(actions))}")
        values[dest] = _config_value(key, value, actions[dest], fail)

    base = cfg_path.resolve().parent

    def rebase(value: str) -> str:
        target = base / value
        try:
            return os.path.relpath(target)
        except ValueError:
            return str(target)

    for dest in _CONFIG_PATH_KEYS & values.keys():
        value = values[dest]
        if isinstance(value, list):
            values[dest] = [rebase(v) for v in value]
        elif value and (dest != "python" or "/" in value or "\\" in value):
            values[dest] = rebase(value)
    return values


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="chisel",
        description="Carve monoliths apart: draw boundaries around features, services "
        "and modules, check nothing leaks out of them, and graph who imports what.",
    )
    ap.add_argument(
        "path",
        nargs="?",
        help='project directory or single .py file (or "path" in the config)',
    )
    ap.add_argument(
        "--config",
        metavar="FILE",
        help="JSON file with default values for any option below; "
        "options given on the command line take precedence",
    )
    ap.add_argument(
        "--dump-config",
        action="store_true",
        help="print the effective settings as a JSON config and exit",
    )
    ap.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="DIR",
        help="directory name to skip (repeatable)",
    )
    ap.add_argument(
        "--no-default-excludes",
        action="store_true",
        help="don't skip venv, .git, __pycache__, build, ...",
    )
    ap.add_argument(
        "--include-stubs", action="store_true", help="also analyse .pyi files"
    )
    ap.add_argument(
        "--no-private",
        action="store_true",
        help="drop definitions whose name has a _private component",
    )
    ap.add_argument(
        "--format",
        choices=("json", "text"),
        help="output format (default: json for the symbol report, "
        "text for --check-boundaries)",
    )
    ap.add_argument("-o", "--output", help="write to this file instead of stdout")
    f = ap.add_argument_group("external imports")
    f.add_argument(
        "--ignore-builtins",
        action="store_true",
        help="drop standard-library imports (os, sys, collections, ...)",
    )
    f.add_argument(
        "--ignore-installed",
        action="store_true",
        help="drop imports of third-party packages installed in the environment",
    )
    f.add_argument(
        "--python",
        metavar="PATH",
        help="interpreter whose installed packages to check against "
        "(default: the one running chisel)",
    )
    b = ap.add_argument_group("boundaries check")
    b.add_argument(
        "--check-boundaries",
        action="store_true",
        help="fail (exit 1) if any file leaks out of a boundary; "
        "--format picks text or json output",
    )
    b.add_argument(
        "--boundaries",
        action="append",
        default=[],
        metavar="FILE",
        help="boundaries file to use (repeatable), in addition to every "
        f"{BOUNDARIES_FILENAME} found in the project",
    )
    b.add_argument(
        "--no-boundaries-discovery",
        action="store_true",
        help=f"only use --boundaries files, don't search for {BOUNDARIES_FILENAME}",
    )
    g = ap.add_argument_group("import graph (Graphviz DOT)")
    g.add_argument(
        "--graph",
        choices=("files", "modules", "boundaries"),
        help="emit an import graph instead of the symbol report: "
        "'files' = one node per file, 'modules' = one node per package, "
        "'boundaries' = boundaries, their interfaces and leaks",
    )
    g.add_argument(
        "--graph-arrows",
        choices=("to-importer", "to-imported"),
        default="to-importer",
        help="arrow direction: 'to-importer' (default) points from the imported "
        "file to its importer; 'to-imported' points the other way",
    )
    g.add_argument(
        "--graph-depth",
        type=int,
        metavar="N",
        help="modules/boundaries mode: collapse packages to their first N components",
    )
    g.add_argument(
        "--graph-external",
        action="store_true",
        help="also show third-party/stdlib imports (by top-level name)",
    )
    g.add_argument(
        "--graph-hide-isolated",
        action="store_true",
        help="leave out files/packages that neither import nor are imported "
        "by anything shown in the graph",
    )
    g.add_argument(
        "--graph-no-type-checking",
        action="store_true",
        help="leave out imports that only exist under `if TYPE_CHECKING:`",
    )
    return ap


def dump_config(args: argparse.Namespace) -> str:
    return json.dumps(
        {k: v for k, v in vars(args).items() if k not in _CONFIG_META}, indent=2
    )


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config")
    known, _ = pre.parse_known_args(argv)
    if known.config:
        parser.set_defaults(**load_config(known.config, parser))
    args = parser.parse_args(argv)

    if args.dump_config:
        print(dump_config(args))
        raise SystemExit(0)
    if args.path is None:
        parser.error(
            'no project path given (pass it as an argument or as "path" in the config)'
        )
    if not Path(args.path).exists():
        parser.error(f"{args.path} does not exist")
    if args.check_boundaries and args.graph:
        parser.error(
            "--check-boundaries and --graph can't be used together "
            "(use --graph boundaries to draw the check's result)"
        )
    return args


def excludes_from(args: argparse.Namespace) -> Set[str]:
    return set(args.exclude) | (set() if args.no_default_excludes else DEFAULT_EXCLUDES)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    root = Path(args.path)
    excludes = excludes_from(args)

    reports = analyze_project(root, excludes, args.include_stubs)

    env = probe_environment(args.python)
    print(
        f"classifying imports with {env.python} (Python {env.version})", file=sys.stderr
    )
    classify_imports(reports, env)
    ignored = ({"stdlib"} if args.ignore_builtins else set()) | (
        {"installed"} if args.ignore_installed else set()
    )
    if ignored:
        drop_imports(reports, ignored)
    if args.no_private:
        for r in reports:
            r.definitions = [d for d in r.definitions if not d.private]

    if args.check_boundaries or args.graph == "boundaries":
        loaded = _load_boundaries(args, root, excludes, reports)
        if isinstance(loaded, int):
            return loaded
        boundaries, warnings = loaded
        violations = check_boundaries(reports, boundaries)
        if args.check_boundaries:
            return _report_boundary_check(
                args, reports, boundaries, violations, warnings
            )
        text = boundary_graph_to_dot(
            build_boundary_graph(reports, boundaries, violations, args.graph_depth),
            args.graph_arrows,
        )
        _emit(text, args.output)
        for w in warnings:
            print(f"warning: {w}", file=sys.stderr)
        print(
            f"{len(boundaries)} boundary(ies), {len(violations)} leak(s) highlighted",
            file=sys.stderr,
        )
        return 0

    if args.graph:
        graph = build_import_graph(
            reports,
            args.graph,
            args.graph_depth,
            args.graph_external,
            not args.graph_no_type_checking,
            args.graph_hide_isolated,
        )
        text = graph_to_dot(graph, args.graph_arrows)
        for cycle in graph.cycles:
            print("import cycle: " + " -> ".join(cycle), file=sys.stderr)
    elif args.format in (None, "json"):
        text = json.dumps([asdict(r) for r in reports], indent=2)
    else:
        text = format_text_report(reports)
    _emit(text, args.output)

    failed = [r for r in reports if r.error]
    for r in failed:
        print(f"warning: could not parse {r.path}: {r.error}", file=sys.stderr)
    print(f"{len(reports)} files analysed, {len(failed)} failed", file=sys.stderr)
    return 0


def _emit(text: str, output: Optional[str]) -> None:
    if output:
        Path(output).write_text(text, encoding="utf-8")
    else:
        print(text)


def _load_boundaries(
    args: argparse.Namespace, root: Path, excludes: Set[str], reports: List[FileReport]
):
    paths = [Path(p) for p in args.boundaries]
    if not args.no_boundaries_discovery:
        paths += list(find_boundary_files(root, excludes))
    unique: dict = {}
    for p in paths:
        unique.setdefault(p.resolve(), p)
    if not unique:
        print(
            f"error: no boundaries to check (no {BOUNDARIES_FILENAME} found and "
            "no --boundaries given)",
            file=sys.stderr,
        )
        return 2
    base = root if root.is_dir() else root.parent
    try:
        boundaries, warnings = load_boundaries(unique.values(), reports, base)
    except BoundaryError as exc:
        print(f"boundaries configuration error:\n{exc}", file=sys.stderr)
        return 2
    return boundaries, warnings


def _report_boundary_check(
    args: argparse.Namespace,
    reports: List[FileReport],
    boundaries: list,
    violations: list,
    warnings: List[str],
) -> int:
    if args.format == "json":
        text = json.dumps(
            {
                "passed": not violations,
                "boundaries": [
                    {
                        "name": b.name,
                        "source": b.source,
                        "files": len(b.modules),
                        "file_interfaces": sorted(b.file_interfaces),
                        "object_interfaces": {
                            m: sorted(o) for m, o in sorted(b.object_interfaces.items())
                        },
                    }
                    for b in boundaries
                ],
                "violations": [asdict(v) for v in violations],
                "warnings": warnings,
            },
            indent=2,
        )
    else:
        text = format_boundary_report(boundaries, violations)
    _emit(text, args.output)

    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    for r in reports:
        if r.error:
            print(
                f"warning: could not parse {r.path}, its imports weren't checked: {r.error}",
                file=sys.stderr,
            )
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
