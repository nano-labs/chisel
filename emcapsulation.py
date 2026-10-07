import ast
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

from .symbols import (
    DEFAULT_EXCLUDES,
    FileReport,
    module_name_for,
    parse_file,
    walk_project,
)

BOUNDARIES_FILENAME = "boundaries.json"
_BOUNDARY_KEYS = {"name", "files", "interfaces", "for_each", "exclude"}


class BoundaryError(Exception):
    """Invalid boundaries configuration."""


@dataclass
class Boundary:
    name: str
    source: str
    modules: Set[str]
    file_interfaces: Set[str]
    object_interfaces: dict
    from_pattern: bool = False
    folder: str = ""
    named: bool = False

    def allows(self, module: str, path: str) -> str:
        """'yes', 'partial' (could lead to an interface), or 'no'."""
        if module in self.file_interfaces:
            return "yes"
        objects = self.object_interfaces.get(module, ())
        if any(path == o or path.startswith(o + ".") for o in objects):
            return "yes"
        if objects and (path == "" or any(o.startswith(path + ".") for o in objects)):
            return "partial"
        return "no"


@dataclass
class Violation:
    path: str
    lineno: int
    module: str
    boundary: str
    target: str
    message: str


def find_boundary_files(
    root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES
) -> Iterator[Path]:
    start = root if root.is_dir() else root.parent
    for dirpath, filenames in walk_project(start, excludes):
        if BOUNDARIES_FILENAME in filenames:
            yield Path(dirpath) / BOUNDARIES_FILENAME


def _expand_paths(base: Path, pattern: str, files_by_path: dict) -> Set[Path]:
    """Analysed .py files matched by a path, directory, or glob (relative to base)."""
    if any(ch in pattern for ch in "*?["):
        try:
            matches = list(base.glob(pattern))
        except (ValueError, NotImplementedError) as exc:
            raise BoundaryError(f"invalid pattern {pattern!r}: {exc}")
    else:
        candidate = base / pattern
        matches = [candidate] if candidate.exists() else []
    found: Set[Path] = set()
    for match in matches:
        match = match.resolve()
        if match.is_dir():
            found.update(f for f in files_by_path if match in f.parents)
        elif match in files_by_path:
            found.add(match)
    return found


def _resolve_interface(
    spec: str,
    base: Path,
    files_by_path: dict,
    modules: Set[str],
    relative_only: bool = False,
) -> List[tuple]:
    file_part, sep, obj = spec.partition(":")
    if sep:
        files = _expand_paths(base, file_part, files_by_path)
        if not files:
            raise BoundaryError(f"interface {spec!r}: no file matches {file_part!r}")
        return [(files_by_path[f].module, obj) for f in files]

    files = _expand_paths(base, spec, files_by_path)
    if files:
        return [(files_by_path[f].module, "") for f in files]

    parts = spec.split(".")
    for i in range(len(parts), 0, -1):
        folder = base.joinpath(*parts[:i])
        for candidate in (
            folder.with_name(folder.name + ".py"),
            folder / "__init__.py",
        ):
            candidate = candidate.resolve()
            if candidate in files_by_path:
                return [(files_by_path[candidate].module, ".".join(parts[i:]))]
    if not relative_only:
        for i in range(len(parts), 0, -1):
            module = ".".join(parts[:i])
            if module in modules:
                return [(module, ".".join(parts[i:]))]
    raise BoundaryError(f"interface {spec!r} doesn't match any file or module")


def _default_boundary_name(folder: Path, root: Path) -> str:
    if (folder / "__init__.py").exists():
        try:
            return module_name_for(folder / "__init__.py", root)[0] or folder.name
        except ValueError:
            pass
    return folder.name


def _string_list(entry: dict, key: str, default: List[str]) -> List[str]:
    value = entry.get(key, default)
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BoundaryError(f"{key!r} must be a string or a list of strings")
    return value


def _pattern_folders(base: Path, entry: dict, files_by_path: dict) -> List[Path]:
    """Folders matched by "for_each" (minus "exclude") that hold analysed files."""
    folders: Set[Path] = set()
    for pattern in _string_list(entry, "for_each", []):
        folders_only = pattern.endswith(("/", "\\"))
        pattern = pattern.rstrip("/\\") or "."
        if any(ch in pattern for ch in "*?["):
            try:
                matches = list(base.glob(pattern))
            except (ValueError, NotImplementedError) as exc:
                raise BoundaryError(f"invalid for_each pattern {pattern!r}: {exc}")
        else:
            matches = [base / pattern] if (base / pattern).exists() else []
        for match in matches:
            if match.is_dir():
                folders.add(match.resolve())
            elif not folders_only:
                folders.add(match.parent.resolve())
    excluded = {(base / e).resolve() for e in _string_list(entry, "exclude", [])}
    return sorted(
        f for f in folders - excluded if any(f in p.parents for p in files_by_path)
    )


def _parse_entry(
    entry,
    source: Path,
    root: Path,
    files_by_path: dict,
    modules: Set[str],
    warnings: List[str],
    where: str,
) -> List[Boundary]:
    if not isinstance(entry, dict):
        raise BoundaryError("each boundary must be a JSON object")
    unknown = sorted(
        k for k in entry if k not in _BOUNDARY_KEYS and not k.startswith("_")
    )
    if unknown:
        raise BoundaryError(
            f"unknown key(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(sorted(_BOUNDARY_KEYS))}"
        )
    name = entry.get("name")
    if name is not None and not isinstance(name, str):
        raise BoundaryError("'name' must be a string")
    base = source.resolve().parent

    if "for_each" not in entry:
        if "exclude" in entry:
            raise BoundaryError("'exclude' only applies together with 'for_each'")
        b = _build_boundary(
            entry,
            name or _default_boundary_name(base, root),
            base,
            files_by_path,
            modules,
            warnings,
            where,
        )
        b.folder, b.named = str(base), bool(name)
        return [b]

    if name and "{name}" not in name:
        raise BoundaryError(
            "'name' in a for_each definition must contain {name}, "
            "or every folder would get the same name"
        )
    folders = _pattern_folders(base, entry, files_by_path)
    if not folders:
        warnings.append(f"{where}: for_each matches no folder with Python files")
    matched: dict = {}
    boundaries = []
    for folder in folders:
        default = _default_boundary_name(folder, root)
        b = _build_boundary(
            entry,
            name.replace("{name}", default) if name else default,
            folder,
            files_by_path,
            modules,
            warnings,
            f"{where} (for {os.path.relpath(folder, base)})",
            matched,
        )
        b.from_pattern, b.folder, b.named = True, str(folder), bool(name)
        boundaries.append(b)
    for spec in _string_list(entry, "interfaces", []):
        if folders and not matched.get(spec):
            warnings.append(
                f"{where}: interface {spec!r} matches in none of the "
                f"{len(folders)} for_each folders"
            )
    return boundaries


def _build_boundary(
    entry: dict,
    name: str,
    base: Path,
    files_by_path: dict,
    modules: Set[str],
    warnings: List[str],
    where: str,
    matched: Optional[dict] = None,
) -> Boundary:
    members: Set[Path] = set()
    for pattern in _string_list(entry, "files", ["."]):
        found = _expand_paths(base, pattern, files_by_path)
        if not found and matched is None:
            warnings.append(
                f"{where}: files entry {pattern!r} matches no analysed Python file"
            )
        members |= found
    boundary_modules = {files_by_path[f].module for f in members}
    if not boundary_modules:
        warnings.append(f"{where}: boundary {name!r} contains no files")

    file_ifaces: Set[str] = set()
    object_ifaces: dict = {}
    by_module = {r.module: r for r in files_by_path.values()}
    for spec in _string_list(entry, "interfaces", []):
        try:
            resolved = _resolve_interface(
                spec, base, files_by_path, modules, relative_only=matched is not None
            )
        except BoundaryError:
            if matched is None:
                raise
            continue
        if matched is not None:
            matched[spec] = matched.get(spec, 0) + 1
        for module, obj in resolved:
            if module not in boundary_modules:
                raise BoundaryError(
                    f"interface {spec!r} ({module}) is not inside boundary {name!r}"
                )
            if not obj:
                file_ifaces.add(module)
                continue
            object_ifaces.setdefault(module, set()).add(obj)
            report = by_module[module]
            defined = {d.name for d in report.definitions}
            imported = {i.bound_as for i in report.imports if i.bound_as}
            if obj not in defined and obj.split(".")[0] not in imported:
                warnings.append(
                    f"{where}: interface {spec!r}: {module} doesn't define {obj}"
                )
    return Boundary(name, where, boundary_modules, file_ifaces, object_ifaces)


def _merge_definitions(boundaries: List[Boundary], errors: List[str]) -> List[Boundary]:
    by_folder = {b.folder: b for b in boundaries if b.from_pattern}
    for b in boundaries:
        if not b.from_pattern and not b.named and b.folder in by_folder:
            b.name = by_folder[b.folder].name
    by_name: dict = {}
    for b in boundaries:
        by_name.setdefault(b.name, []).append(b)
    result = []
    for name, group in by_name.items():
        if len(group) == 1:
            result.append(group[0])
            continue
        patterns = [b for b in group if b.from_pattern]
        plain = [b for b in group if not b.from_pattern]
        if len(patterns) != 1 or len(plain) != 1:
            errors.append(
                f"duplicate boundary name {name!r} ({' and '.join(b.source for b in group)}); "
                'set a distinct "name" in one of them'
            )
            continue
        base, extra = patterns[0], plain[0]
        base.modules |= extra.modules
        base.file_interfaces |= extra.file_interfaces
        for module, objects in extra.object_interfaces.items():
            base.object_interfaces.setdefault(module, set()).update(objects)
        base.source = f"{base.source} + {extra.source}"
        result.append(base)
    return result


def load_boundaries(
    paths: Iterable[Path], reports: List[FileReport], root: Path
) -> tuple:
    files_by_path = {Path(r.path).resolve(): r for r in reports if r.module}
    modules = {r.module for r in reports if r.module}
    boundaries: List[Boundary] = []
    errors: List[str] = []
    warnings: List[str] = []
    for path in paths:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except OSError as exc:
            errors.append(f"{path}: {exc.strerror or exc}")
            continue
        except json.JSONDecodeError as exc:
            errors.append(
                f"{path}: invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            )
            continue
        entries = data if isinstance(data, list) else [data]
        for i, entry in enumerate(entries):
            where = f"{path}[{i}]" if isinstance(data, list) else str(path)
            try:
                boundaries.extend(
                    _parse_entry(
                        entry, Path(path), root, files_by_path, modules, warnings, where
                    )
                )
            except BoundaryError as exc:
                errors.append(f"{where}: {exc}")

    boundaries = _merge_definitions(boundaries, errors)
    if errors:
        raise BoundaryError("\n".join(errors))
    return boundaries, warnings


def check_boundaries(
    reports: List[FileReport], boundaries: List[Boundary]
) -> List[Violation]:
    modules = {r.module: r for r in reports if r.module and not r.error}
    owners: dict = {}
    for b in boundaries:
        for m in b.modules:
            owners.setdefault(m, []).append(b)

    def split(full: str) -> tuple:
        parts = full.split(".")
        for i in range(len(parts), 0, -1):
            module = ".".join(parts[:i])
            if module in modules:
                return module, ".".join(parts[i:])
        return None, ""

    def foreign(importer: str, target: Optional[str]) -> List[Boundary]:
        """Boundaries that contain the target but not the importer."""
        mine = {b.name for b in owners.get(importer, [])}
        return [b for b in owners.get(target, []) if b.name not in mine]

    violations: List[Violation] = []
    for r in modules.values():
        bindings: dict = {}  # local name -> dotted bases to follow through uses
        for imp in r.imports:
            if imp.project_module is None or not imp.resolved_name:
                continue
            full = (
                f"{imp.resolved_name}.{imp.name}"
                if imp.kind == "from"
                else imp.resolved_name
            )
            target, path = split(full)
            note = " (TYPE_CHECKING)" if imp.type_checking_only else ""
            for b in foreign(r.module, target):
                verdict = b.allows(target, path)
                if verdict == "yes":
                    continue
                if imp.kind == "star":
                    msg = (
                        f"star-imports {target} from boundary '{b.name}'; "
                        "only allowed when the whole file is an interface"
                    )
                elif (
                    verdict == "partial"
                    and imp.bound_as
                    and imp.kind in ("import", "from")
                ):
                    if imp.kind == "import" and not imp.asname:
                        # `import a.b.c` binds `a`: follow uses from the top name
                        extra = len(imp.resolved_name.split(".")) - len(
                            imp.module.split(".")
                        )
                        base = ".".join(imp.resolved_name.split(".")[: extra + 1])
                    else:
                        base = full
                    bindings.setdefault(imp.bound_as, set()).add(base)
                    continue
                elif imp.kind == "dynamic":
                    msg = f"dynamically imports {full} from boundary '{b.name}', which isn't an interface"
                else:
                    msg = f"imports {full} from boundary '{b.name}', which isn't part of its interface"
                violations.append(
                    Violation(r.path, imp.lineno, r.module, b.name, full, msg + note)
                )
        if bindings:
            violations.extend(_check_uses(r, bindings, split, foreign))

    unique = {(v.path, v.lineno, v.boundary, v.target): v for v in violations}
    return sorted(unique.values(), key=lambda v: (v.path, v.lineno, v.target))


def _check_uses(report: FileReport, bindings: dict, split, foreign) -> List[Violation]:
    """Follow `name.attr.attr` chains of names bound to boundary objects."""
    tree = parse_file(report.path)
    if tree is None:
        return []
    inner = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    out: List[Violation] = []
    for node in ast.walk(tree):
        if id(node) in inner or not isinstance(node, (ast.Attribute, ast.Name)):
            continue
        attrs = []
        current = node
        while isinstance(current, ast.Attribute):
            attrs.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name) or current.id not in bindings:
            continue
        attrs.reverse()
        for base in bindings[current.id]:
            full = ".".join([base] + attrs)
            target, path = split(full)
            for b in foreign(report.module, target):
                if b.allows(target, path) != "yes":
                    used = ".".join([current.id] + attrs)
                    shown = used if used == full else f"{used} ({full})"
                    out.append(
                        Violation(
                            report.path,
                            node.lineno,
                            report.module,
                            b.name,
                            full,
                            f"uses {shown} from boundary '{b.name}', "
                            "which isn't part of its interface",
                        )
                    )
    return out


def format_boundary_report(
    boundaries: List[Boundary], violations: List[Violation]
) -> str:
    lines = [f"{v.path}:{v.lineno}: [{v.boundary}] {v.message}" for v in violations]
    files = len({v.path for v in violations})
    if violations:
        lines.append(
            f"\nBoundary check FAILED: {len(violations)} violation(s) in {files} "
            f"file(s), {len(boundaries)} boundary(ies) checked"
        )
    else:
        lines.append(
            f"Boundary check passed: {len(boundaries)} boundary(ies), "
            f"{sum(len(b.modules) for b in boundaries)} file(s) protected"
        )
    return "\n".join(lines)
