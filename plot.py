from dataclasses import dataclass
from typing import Iterable, List, Optional

from .symbols import FileReport, ImportedObject, resolve_project_imports

_CATEGORY_ORDER = ("runtime", "lazy", "dynamic", "type_checking")
_EDGE_STYLE = {
    "runtime": 'color="#444444"',
    "lazy": 'color="#1f77b4", style=dashed',
    "dynamic": 'color="#2ca02c", style=dashed',
    "type_checking": 'color="#aaaaaa", style=dotted',
}

_EXTERNAL_NODE_STYLE = {
    "stdlib": [
        "shape=ellipse",
        'style="dashed"',
        'color="#999999"',
        'fontcolor="#666666"',
    ],
    "installed": [
        "shape=ellipse",
        'style="filled"',
        'fillcolor="#e3eefa"',
        'color="#6b9bd1"',
    ],
    "unknown": [
        "shape=ellipse",
        'style="dashed"',
        'color="#e6550d"',
        'fontcolor="#e6550d"',
    ],
    None: ["shape=ellipse", 'style="dashed"', 'fontcolor="#666666"'],
}


@dataclass
class ImportGraph:
    mode: str
    nodes: dict  # node id -> {"label", "external", "cluster"}
    edges: dict  # (src, dst) -> {"count", "categories", "lines"}
    cycles: List[List[str]]


def _import_category(imp: ImportedObject) -> str:
    if imp.kind == "dynamic":
        return "dynamic"
    if imp.type_checking_only:
        return "type_checking"
    if imp.lazy:
        return "lazy"
    return "runtime"


def _package_of(module: str, is_package: bool) -> str:
    return module if is_package else (module.rpartition(".")[0] or module)


def _strongly_connected(nodes: Iterable[str], adj: dict) -> List[List[str]]:
    """Iterative Tarjan: returns components with more than one node (cycles)."""
    index, low, on_stack, stack, result = {}, {}, set(), [], []
    counter = 0
    for root in nodes:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        work = [(root, iter(adj.get(root, ())))]
        while work:
            v, children = work[-1]
            for w in children:
                if w not in index:
                    index[w] = low[w] = counter
                    counter += 1
                    stack.append(w)
                    on_stack.add(w)
                    work.append((w, iter(adj.get(w, ()))))
                    break
                if w in on_stack:
                    low[v] = min(low[v], index[w])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    component = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        component.append(w)
                        if w == v:
                            break
                    if len(component) > 1:
                        result.append(sorted(component))
    return result


def build_import_graph(
    reports: List[FileReport],
    mode: str = "files",
    depth: Optional[int] = None,
    include_external: bool = False,
    include_type_checking: bool = True,
    hide_isolated: bool = False,
) -> ImportGraph:
    """mode="files": one node per file. mode="modules": files collapsed into
    their package, optionally truncated to the first `depth` components."""
    resolve_project_imports(reports)
    modules = {r.module: r for r in reports if r.module and not r.error}

    def group(module: str) -> str:
        if mode == "files":
            return module
        pkg = _package_of(module, modules[module].is_package)
        return ".".join(pkg.split(".")[:depth]) if depth else pkg

    nodes: dict = {}
    for module, r in modules.items():
        node = group(module)
        if mode == "files":
            leaf = "__init__.py" if r.is_package else module.rpartition(".")[2] + ".py"
            nodes[node] = {
                "label": leaf,
                "external": False,
                "cluster": (
                    _package_of(module, r.is_package)
                    if "." in module or r.is_package
                    else None
                ),
                "path": r.path,
            }
        else:
            nodes.setdefault(node, {"label": node, "external": False, "cluster": None})

    edges: dict = {}
    for module, r in modules.items():
        src = group(module)
        for imp in r.imports:
            category = _import_category(imp)
            if category == "type_checking" and not include_type_checking:
                continue
            if imp.project_module is not None:
                dst = group(imp.project_module)
            elif include_external and imp.module:
                top = imp.module.split(".")[0]
                dst = "ext:" + top
                nodes.setdefault(
                    dst,
                    {
                        "label": top,
                        "external": True,
                        "cluster": None,
                        "origin": imp.origin,
                    },
                )
            else:
                continue
            if dst == src:
                continue
            edge = edges.setdefault(
                (src, dst), {"count": 0, "categories": set(), "lines": []}
            )
            edge["count"] += 1
            edge["categories"].add(category)
            edge["lines"].append(imp.lineno)

    if hide_isolated:
        # a node survives only if some edge that is actually drawn touches it,
        # so the result respects --graph-external / --ignore-* / type-checking filters
        connected = {n for edge in edges for n in edge}
        nodes = {n: info for n, info in nodes.items() if n in connected}

    runtime_adj: dict = {}
    for (src, dst), e in edges.items():
        if "runtime" in e["categories"] and not nodes[dst]["external"]:
            runtime_adj.setdefault(src, []).append(dst)
    cycles = _strongly_connected(sorted(nodes), runtime_adj)
    return ImportGraph(mode, nodes, edges, cycles)


def _dot_quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


ARROW_DIRECTIONS = ("to-importer", "to-imported")


def _arrow(src: str, dst: str, arrows: str) -> str:
    """DOT edge for "src imports dst". By default the arrow points from the
    imported file to its importer, following how code and data flow."""
    if arrows not in ARROW_DIRECTIONS:
        raise ValueError(f"arrows must be one of {ARROW_DIRECTIONS}, not {arrows!r}")
    tail, head = (dst, src) if arrows == "to-importer" else (src, dst)
    return f"{_dot_quote(tail)} -> {_dot_quote(head)}"


def _direction_comment(arrows: str) -> str:
    return (
        "  // arrows point from the imported file to the file importing it"
        if arrows == "to-importer"
        else "  // arrows point from the importing file to the file it imports"
    )


def graph_to_dot(graph: ImportGraph, arrows: str = "to-importer") -> str:
    in_cycle = {n: i for i, comp in enumerate(graph.cycles) for n in comp}
    out = [
        "digraph imports {",
        '  graph [rankdir=LR, fontname="Helvetica", fontsize=11, nodesep=0.25, ranksep=0.6];',
        '  node [shape=box, style="rounded,filled", fillcolor="#f4f4f4", color="#888888",'
        ' fontname="Helvetica", fontsize=10];',
        '  edge [arrowsize=0.6, fontname="Helvetica", fontsize=8];',
        "  // edges: solid=runtime, dashed blue=lazy (in function), dashed green=dynamic,",
        "  //        dotted grey=TYPE_CHECKING only, red=part of a runtime import cycle",
        _direction_comment(arrows),
        "  // external: grey dashed=stdlib, blue=installed, orange=not found in environment",
    ]

    def node_line(node_id: str, indent: str) -> str:
        info = graph.nodes[node_id]
        attrs = [f"label={_dot_quote(info['label'])}"]
        if info["external"]:
            attrs += _EXTERNAL_NODE_STYLE.get(
                info.get("origin"), _EXTERNAL_NODE_STYLE[None]
            )
        elif node_id in in_cycle:
            attrs += ['fillcolor="#fde0dd"', 'color="#d62728"']
        tooltip = info.get("path") or (
            f"{info['label']} ({info.get('origin') or 'external'})"
            if info["external"]
            else node_id
        )
        attrs.append(f"tooltip={_dot_quote(tooltip)}")
        return f"{indent}{_dot_quote(node_id)} [{', '.join(attrs)}];"

    if graph.mode == "files":
        # nested clusters mirroring the package tree
        tree: dict = {}
        loose: List[str] = []
        for node_id, info in sorted(graph.nodes.items()):
            if info["external"] or not info["cluster"]:
                loose.append(node_id)
                continue
            branch = tree
            for part in info["cluster"].split("."):
                branch = branch.setdefault(part, {})
            branch.setdefault("__nodes__", []).append(node_id)

        def emit(branch: dict, path: List[str], indent: str) -> None:
            for name in sorted(k for k in branch if k != "__nodes__"):
                sub_path = path + [name]
                cluster_id = "cluster_" + "_".join(sub_path).replace("-", "_")
                out.append(f"{indent}subgraph {_dot_quote(cluster_id)} {{")
                out.append(f"{indent}  label={_dot_quote('.'.join(sub_path))};")
                out.append(
                    f'{indent}  style="rounded"; color="#bbbbbb"; fontcolor="#555555";'
                )
                for node_id in branch[name].get("__nodes__", []):
                    out.append(node_line(node_id, indent + "  "))
                emit(branch[name], sub_path, indent + "  ")
                out.append(f"{indent}}}")

        emit(tree, [], "  ")
        for node_id in loose:
            out.append(node_line(node_id, "  "))
    else:
        for node_id in sorted(graph.nodes):
            out.append(node_line(node_id, "  "))

    for (src, dst), e in sorted(graph.edges.items()):
        category = next(c for c in _CATEGORY_ORDER if c in e["categories"])
        attrs = [_EDGE_STYLE[category]]
        penwidth = 1.0
        if (
            in_cycle.get(src) is not None
            and in_cycle.get(src) == in_cycle.get(dst)
            and "runtime" in e["categories"]
        ):
            attrs = ['color="#d62728"']
            penwidth = 1.6
        if graph.mode == "modules" and e["count"] > 1:
            attrs.append(f"label={e['count']}")
            penwidth = max(penwidth, min(1 + e["count"] ** 0.5 / 2, 5))
        if penwidth != 1.0:
            attrs.append(f"penwidth={penwidth:.1f}")
        lines = ", ".join(map(str, sorted(set(e["lines"]))[:20]))
        attrs.append(f"tooltip={_dot_quote(f'{src} imports {dst} (lines {lines})')}")
        out.append(f"  {_arrow(src, dst, arrows)} [{', '.join(attrs)}];")

    out.append("}")
    return "\n".join(out) + "\n"
