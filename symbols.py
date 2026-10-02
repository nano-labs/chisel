from __future__ import annotations

import ast
import os
import tokenize
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, List, Optional, Set

DEFAULT_EXCLUDES = {
    "__pycache__",
    ".mypy_cache",
    "build",
    "dist",
    ".eggs",
}


@dataclass
class Definition:
    name: str
    kind: str
    lineno: int
    private: bool
    conditional: bool
    redefined_at: List[int] = field(default_factory=list)


@dataclass
class ImportedObject:
    kind: str
    module: Optional[str]
    name: Optional[str]
    asname: Optional[str]
    bound_as: Optional[str]
    source: str
    level: int
    lineno: int
    scope: str
    type_checking_only: bool
    conditional: bool
    lazy: bool
    project_module: Optional[str] = None
    resolution: Optional[str] = None
    origin: Optional[str] = None
    resolved_name: Optional[str] = None


@dataclass
class FileReport:
    path: str
    module: str
    is_package: bool
    definitions: List[Definition]
    imports: List[ImportedObject]
    all_exports: Optional[List[str]]
    error: Optional[str] = None


def _is_private(qualname: str) -> bool:
    for part in qualname.split("."):
        if part.startswith("_") and not (part.startswith("__") and part.endswith("__")):
            return True
    return False


def _decorator_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        node = node.func
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return ""


def _is_type_checking(test: ast.expr) -> bool:
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _literal_str_list(node: ast.AST) -> Optional[List[str]]:
    try:
        value = ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
        return None
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def resolve_relative(
    current_module: str, is_package: bool, target: Optional[str], level: int
) -> Optional[str]:
    if level == 0:
        return target
    parts = current_module.split(".") if current_module else []
    if not is_package:
        parts = parts[:-1]
    if level - 1 > len(parts):
        return None  # goes above the top-level package
    base = parts[: len(parts) - (level - 1)]
    if target:
        base += target.split(".")
    return ".".join(base) or None


def module_name_for(path: Path, root: Path) -> tuple:
    base = root.resolve()
    while (base / "__init__.py").exists() and base.parent != base:
        base = base.parent
    rel = path.resolve().relative_to(base).with_suffix("")
    parts = list(rel.parts)
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


@dataclass
class _Scope:
    kind: str
    name: str
    globals: Set[str] = field(default_factory=set)
    self_name: Optional[str] = None
    self_kind: Optional[str] = None


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, is_package: bool) -> None:
        self.module = module
        self.is_package = is_package
        self.scopes: List[_Scope] = [_Scope("module", "<module>")]
        self.defs: dict = {}
        self.imports: List[ImportedObject] = []
        self.all_exports: Optional[List[str]] = None
        self._cond = 0
        self._type_checking = 0

    def _accessible(self) -> bool:
        return all(s.kind != "function" for s in self.scopes)

    def _prefix(self) -> List[str]:
        return [s.name for s in self.scopes[1:]]

    def _scope_label(self) -> str:
        return ".".join(self._prefix()) or "<module>"

    def _at_module(self) -> bool:
        return len(self.scopes) == 1

    def _record(self, qualname: str, kind: str, lineno: int, conditional: bool) -> None:
        existing = self.defs.get(qualname)
        if existing is not None:
            existing.redefined_at.append(lineno)
            if not conditional:
                existing.conditional = False
            return
        self.defs[qualname] = Definition(
            qualname, kind, lineno, _is_private(qualname), conditional
        )

    def _bind(self, name: str, kind: str, node: ast.AST) -> None:
        scope = self.scopes[-1]
        if scope.kind == "function":
            if name in scope.globals:
                self._record(name, kind, node.lineno, conditional=True)
            return
        if not self._accessible():
            return
        self._record(
            ".".join(self._prefix() + [name]), kind, node.lineno, self._cond > 0
        )

    def _bind_target(self, target: ast.AST, kind: str = "variable") -> None:
        if isinstance(target, ast.Name):
            self._bind(target.id, kind, target)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._bind_target(elt, kind)
        elif isinstance(target, ast.Starred):
            self._bind_target(target.value, kind)
        elif isinstance(target, ast.Attribute):
            self._maybe_member_attr(target)

    def _maybe_member_attr(self, target: ast.Attribute) -> None:
        scope = self.scopes[-1]
        if (
            scope.kind == "function"
            and scope.self_name
            and isinstance(target.value, ast.Name)
            and target.value.id == scope.self_name
            and len(self.scopes) >= 2
            and self.scopes[-2].kind == "class"
            and all(s.kind != "function" for s in self.scopes[:-1])
        ):
            qual = ".".join(self._prefix()[:-1] + [target.attr])
            conditional = scope.name != "__init__" or self._cond > 0
            self._record(
                qual,
                scope.self_kind or "instance_attribute",
                target.lineno,
                conditional,
            )

    def _add_import(self, **kw) -> None:
        self.imports.append(
            ImportedObject(
                scope=self._scope_label(),
                type_checking_only=self._type_checking > 0,
                conditional=self._cond > 0 or not self._accessible(),
                lazy=not self._accessible(),
                **kw,
            )
        )

    def _visit_function(self, node, is_async: bool) -> None:
        for d in node.decorator_list:
            self.visit(d)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)

        decos = {_decorator_name(d) for d in node.decorator_list}
        parent = self.scopes[-1]
        kind: Optional[str]
        if parent.kind == "class":
            if decos & {"setter", "deleter"}:
                kind = None
            elif decos & {"property", "cached_property"}:
                kind = "property"
            elif "staticmethod" in decos:
                kind = "staticmethod"
            elif "classmethod" in decos:
                kind = "classmethod"
            else:
                kind = "async_method" if is_async else "method"
        else:
            kind = "async_function" if is_async else "function"
        if kind:
            self._bind(node.name, kind, node)

        self_name = self_kind = None
        if parent.kind == "class" and "staticmethod" not in decos:
            params = list(getattr(node.args, "posonlyargs", [])) + list(node.args.args)
            if params:
                self_name = params[0].arg
                self_kind = (
                    "class_attribute"
                    if "classmethod" in decos
                    else "instance_attribute"
                )

        self.scopes.append(
            _Scope("function", node.name, self_name=self_name, self_kind=self_kind)
        )
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_FunctionDef(self, node):
        self._visit_function(node, is_async=False)

    def visit_AsyncFunctionDef(self, node):
        self._visit_function(node, is_async=True)

    def visit_Lambda(self, node):
        self.visit(node.args)
        self.scopes.append(_Scope("function", "<lambda>"))
        self.visit(node.body)
        self.scopes.pop()

    def visit_ClassDef(self, node):
        for d in node.decorator_list:
            self.visit(d)
        for b in node.bases:
            self.visit(b)
        for k in node.keywords:
            self.visit(k)
        self._bind(node.name, "class", node)
        self.scopes.append(_Scope("class", node.name))
        for stmt in node.body:
            self.visit(stmt)
        self.scopes.pop()

    def visit_Assign(self, node):
        self.visit(node.value)
        for t in node.targets:
            self._bind_target(t)
        if self._at_module() and any(
            isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
        ):
            self.all_exports = _literal_str_list(node.value)

    def visit_AnnAssign(self, node):
        if node.value:
            self.visit(node.value)
        self.visit(node.annotation)
        if isinstance(node.target, ast.Name):
            self._bind(
                node.target.id, "variable" if node.value else "annotation", node.target
            )
        else:
            self._bind_target(node.target)

    def visit_AugAssign(self, node):
        self.visit(node.value)
        self._bind_target(node.target)
        if (
            self._at_module()
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
            and self.all_exports is not None
        ):
            extra = _literal_str_list(node.value)
            if extra is not None:
                self.all_exports.extend(extra)

    def visit_NamedExpr(self, node):
        self.visit(node.value)
        self._bind_target(node.target)

    def visit_TypeAlias(self, node):
        self._bind(node.name.id, "type_alias", node)
        self.generic_visit(node)

    def visit_Global(self, node):
        if self.scopes[-1].kind == "function":
            self.scopes[-1].globals.update(node.names)

    def visit_Delete(self, node):
        for t in node.targets:
            if isinstance(t, ast.Name) and self._accessible() and self._cond == 0:
                self.defs.pop(".".join(self._prefix() + [t.id]), None)
            else:
                self.visit(t)

    def visit_Expr(self, node):
        call = node.value
        if (
            self._at_module()
            and self.all_exports is not None
            and isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "__all__"
            and call.args
        ):
            if call.func.attr == "extend":
                extra = _literal_str_list(call.args[0])
                if extra is not None:
                    self.all_exports.extend(extra)
            elif call.func.attr == "append":
                extra = _literal_str_list(ast.List(elts=[call.args[0]], ctx=ast.Load()))
                if extra is not None:
                    self.all_exports.extend(extra)
        self.generic_visit(node)

    def visit_If(self, node):
        self.visit(node.test)
        tc = _is_type_checking(node.test)
        self._cond += 1
        self._type_checking += tc
        for stmt in node.body:
            self.visit(stmt)
        self._type_checking -= tc
        for stmt in node.orelse:
            self.visit(stmt)
        self._cond -= 1

    def _visit_for(self, node):
        self.visit(node.iter)
        self._cond += 1
        self._bind_target(node.target)
        for stmt in node.body + node.orelse:
            self.visit(stmt)
        self._cond -= 1

    visit_For = visit_AsyncFor = _visit_for

    def _visit_conditional_block(self, node):
        self._cond += 1
        self.generic_visit(node)
        self._cond -= 1

    visit_While = visit_Try = visit_TryStar = _visit_conditional_block

    def _visit_with(self, node):
        for item in node.items:
            self.visit(item.context_expr)
            if item.optional_vars is not None:
                self._bind_target(item.optional_vars)
        for stmt in node.body:
            self.visit(stmt)

    visit_With = visit_AsyncWith = _visit_with

    def visit_Match(self, node):
        self.visit(node.subject)
        for case in node.cases:
            self._cond += 1
            for sub in ast.walk(case.pattern):
                name = (
                    getattr(sub, "name", None)
                    if type(sub).__name__ in ("MatchAs", "MatchStar")
                    else None
                )
                if type(sub).__name__ == "MatchMapping":
                    name = sub.rest
                if name:
                    self._bind(name, "variable", sub)
            if case.guard:
                self.visit(case.guard)
            for stmt in case.body:
                self.visit(stmt)
            self._cond -= 1

    def visit_Import(self, node):
        for alias in node.names:
            self._add_import(
                kind="import",
                module=alias.name,
                name=None,
                asname=alias.asname,
                bound_as=alias.asname or alias.name.split(".")[0],
                source=alias.name,
                level=0,
                lineno=node.lineno,
            )

    def visit_ImportFrom(self, node):
        level = node.level or 0
        resolved = resolve_relative(self.module, self.is_package, node.module, level)
        source = "." * level + (node.module or "")
        for alias in node.names:
            star = alias.name == "*"
            self._add_import(
                kind="star" if star else "from",
                module=resolved,
                name=alias.name,
                asname=alias.asname,
                bound_as=None if star else (alias.asname or alias.name),
                source=source,
                level=level,
                lineno=node.lineno,
            )

    def visit_Call(self, node):
        fn = node.func
        is_dynamic = (
            isinstance(fn, ast.Name) and fn.id in ("__import__", "import_module")
        ) or (isinstance(fn, ast.Attribute) and fn.attr == "import_module")
        if (
            is_dynamic
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            source = node.args[0].value
            stripped = source.lstrip(".")
            level = len(source) - len(stripped)
            self._add_import(
                kind="dynamic",
                module=resolve_relative(
                    self.module, self.is_package, stripped or None, level
                ),
                name=None,
                asname=None,
                bound_as=None,
                source=source,
                level=level,
                lineno=node.lineno,
            )
        self.generic_visit(node)


def analyze_source(
    source: str, *, path: str = "<string>", module: str = "", is_package: bool = False
) -> FileReport:
    tree = ast.parse(source, filename=path)
    collector = _Collector(module, is_package)
    collector.visit(tree)
    definitions = sorted(collector.defs.values(), key=lambda d: (d.lineno, d.name))
    return FileReport(
        path, module, is_package, definitions, collector.imports, collector.all_exports
    )


def analyze_file(path: Path, root: Path) -> FileReport:
    module, is_package = module_name_for(path, root)
    try:
        with tokenize.open(path) as fh:  # honours PEP 263 encoding cookies / BOM
            source = fh.read()
        return analyze_source(
            source, path=str(path), module=module, is_package=is_package
        )
    except (
        SyntaxError,
        UnicodeDecodeError,
        ValueError,
        RecursionError,
        OSError,
    ) as exc:
        return FileReport(
            str(path),
            module,
            is_package,
            [],
            [],
            None,
            error=f"{type(exc).__name__}: {exc}",
        )


def iter_python_files(
    root: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES, include_stubs: bool = False
) -> Iterator[Path]:
    suffixes = (".py", ".pyi") if include_stubs else (".py",)
    if root.is_file():
        yield root
        return
    for dirpath, filenames in walk_project(root, excludes):
        for fname in filenames:
            if fname.endswith(suffixes):
                yield Path(dirpath) / fname


def _resolve_in_project(
    imp: ImportedObject, report: FileReport, modules: dict
) -> tuple:
    if not imp.module:
        return None, None, None
    target = imp.module.split(".")
    if imp.level:
        prefixes = [[]]
    else:
        parts = [p for p in report.module.split(".") if p]
        pkg = parts if report.is_package else parts[:-1]
        prefixes = [[]] + [pkg[:i] for i in range(len(pkg), 0, -1)]

    for prefix in prefixes:
        how = "relative" if imp.level else ("absolute" if not prefix else "script_dir")
        full = prefix + target
        if imp.kind == "from" and ".".join(full + [imp.name]) in modules:
            return ".".join(full + [imp.name]), how, ".".join(full)
        for i in range(len(full), len(prefix), -1):
            candidate = ".".join(full[:i])
            if candidate in modules:
                return candidate, how, ".".join(full)
    return None, None, None


def resolve_project_imports(reports: List[FileReport]) -> None:
    modules = {r.module: r for r in reports if r.module and not r.error}
    for r in reports:
        for imp in r.imports:
            imp.project_module, imp.resolution, imp.resolved_name = _resolve_in_project(
                imp, r, modules
            )


def analyze_project(
    root, excludes: Iterable[str] = DEFAULT_EXCLUDES, include_stubs: bool = False
) -> List[FileReport]:
    root = Path(root)
    base = root.parent if root.is_file() else root
    reports = [
        analyze_file(p, base) for p in iter_python_files(root, excludes, include_stubs)
    ]
    resolve_project_imports(reports)
    return reports


def walk_project(
    start: Path, excludes: Iterable[str] = DEFAULT_EXCLUDES
) -> Iterator[tuple]:
    excludes = set(excludes)
    for dirpath, dirnames, filenames in os.walk(start):
        dirnames[:] = sorted(
            d for d in dirnames if d not in excludes and not d.endswith(".egg-info")
        )
        yield dirpath, sorted(filenames)


def parse_file(path: str) -> Optional[ast.AST]:
    try:
        with tokenize.open(path) as fh:
            return ast.parse(fh.read(), filename=path)
    except (SyntaxError, UnicodeDecodeError, ValueError, RecursionError, OSError):
        return None


def format_text_report(reports: List[FileReport]) -> str:
    out = []
    for r in reports:
        out.append(f"{r.path}  ({r.module or '<root>'})")
        if r.error:
            out.append(f"  ERROR: {r.error}\n")
            continue
        out.append("  definitions:")
        for d in r.definitions:
            flags = "".join([" ?" if d.conditional else "", " _" if d.private else ""])
            out.append(f"    L{d.lineno:<5} {d.kind:<18} {d.name}{flags}")
        out.append("  imports:")
        for i in r.imports:
            what = i.module or i.source
            if i.name:
                what += f" : {i.name}"
            if i.asname:
                what += f" as {i.asname}"
            if i.project_module and i.project_module != i.module:
                what += f"  -> {i.project_module}"
            tags = [
                t
                for t, on in (
                    ("TYPE_CHECKING", i.type_checking_only),
                    ("via script dir", i.resolution == "script_dir"),
                    (i.origin or "", i.origin not in (None, "project")),
                    (f"in {i.scope}", i.scope != "<module>"),
                    ("dynamic", i.kind == "dynamic"),
                )
                if on
            ]
            out.append(
                f"    L{i.lineno:<5} {what}"
                + (f"  [{', '.join(tags)}]" if tags else "")
            )
        if r.all_exports is not None:
            out.append(f"  __all__: {', '.join(r.all_exports)}")
        out.append("")
    return "\n".join(out)
