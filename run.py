import argparse
import json
import os
import sys
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set
from symbols import analyze_project, format_text_report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        description="Static inventory of what every Python file in a project "
        "defines and imports."
    )
    ap.add_argument("path", help="project directory or single .py file")
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
    ap.add_argument("--format", choices=("json", "text"), default="json")
    ap.add_argument("-o", "--output", help="write to this file instead of stdout")
    args = ap.parse_args(argv)

    root = Path(args.path)
    if not root.exists():
        ap.error(f"{root} does not exist")
    excludes = set(args.exclude) | (
        set() if args.no_default_excludes else DEFAULT_EXCLUDES
    )

    reports = analyze_project(root, excludes, args.include_stubs)
    if args.no_private:
        for r in reports:
            r.definitions = [d for d in r.definitions if not d.private]

    text = (
        json.dumps([asdict(r) for r in reports], indent=2)
        if args.format == "json"
        else format_text_report(reports)
    )
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    else:
        print(text)

    failed = [r for r in reports if r.error]
    for r in failed:
        print(f"warning: could not parse {r.path}: {r.error}", file=sys.stderr)
    print(f"{len(reports)} files analysed, {len(failed)} failed", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
