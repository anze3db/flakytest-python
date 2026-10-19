"""Call trees over a profile document's interned stacks, for the text report and MCP tools."""

from dataclasses import dataclass, field
from typing import Any

HIDDEN_KINDS = ("framework", "native")


@dataclass
class Node:
    frame: int | None
    seconds: float = 0.0
    self_seconds: float = 0.0
    children: dict[int, "Node"] = field(default_factory=dict)


def stack_frames(doc: dict[str, Any], node: int, cache: dict[int, tuple[int, ...]]) -> tuple[int, ...]:
    result = cache.get(node)
    if result is None:
        parent, frame = doc["stacks"][node]
        result = (*stack_frames(doc, parent, cache), frame) if parent >= 0 else (frame,)
        cache[node] = result
    return result


def weighted_stacks(doc: dict[str, Any], tests: list[dict[str, Any]], phases: tuple[str, ...] | None = None):
    """(frame ids from the root, estimated seconds) for every stack of the given tests."""
    cache: dict[int, tuple[int, ...]] = {}
    for test in tests:
        for phase, stacks in test["stacks"].items():
            if phases and phase not in phases:
                continue
            total = test["samples"].get(phase) or 0
            if not total:
                continue
            threads = test.get("thread_stacks", {}).get(phase)
            categories = doc.get("stack_categories", {})
            for node, count in stacks:
                if threads and categories.get(str(node)) == "waiting":
                    continue  # the background threads' work below stands in for this waiting
                yield stack_frames(doc, node, cache), count / total * test["phases"][phase]
            for node, seconds in threads or ():
                yield stack_frames(doc, node, cache), seconds


def build_tree(doc: dict[str, Any], tests: list[dict[str, Any]], *, hide_framework: bool = True) -> Node:
    root = Node(None)
    frames = doc["frames"]
    for path, seconds in weighted_stacks(doc, tests):
        visible = [f for f in path if not (hide_framework and frames[f][3] in HIDDEN_KINDS)] or list(path[-1:])
        root.seconds += seconds
        node = root
        for frame in visible:
            node = node.children.setdefault(frame, Node(frame))
            node.seconds += seconds
        node.self_seconds += seconds
    return root


def frame_label(doc: dict[str, Any], frame: int) -> str:
    path, func, line, _ = doc["frames"][frame]
    return f"{func}  {path}:{line}"


def render_tree(doc: dict[str, Any], root: Node, *, min_share: float = 0.02, max_depth: int = 40) -> list[str]:
    lines: list[str] = []
    total = root.seconds or 1.0

    def walk(node: Node, depth: int, prefix: str) -> None:
        children = sorted(node.children.values(), key=lambda n: -n.seconds)
        shown = [c for c in children if c.seconds / total >= min_share]
        hidden = sum(c.seconds for c in children) - sum(c.seconds for c in shown)
        for i, child in enumerate(shown):
            last = i == len(shown) - 1 and hidden <= 0
            branch = "└─ " if last else "├─ "
            source = doc.get("sources", {}).get(str(child.frame), "")
            label = frame_label(doc, child.frame)  # type: ignore[arg-type]
            code = f"  `{source}`" if source else ""
            lines.append(f"{prefix}{branch}{fmt(child.seconds)} {child.seconds / total:4.0%}  {label}{code}")
            if depth < max_depth:
                walk(child, depth + 1, prefix + ("   " if last else "│  "))
        if hidden > 0 and shown:
            lines.append(f"{prefix}└─ {fmt(hidden)} {hidden / total:4.0%}  … smaller calls")

    walk(root, 0, "")
    return lines


def hot_functions(doc: dict[str, Any], tests: list[dict[str, Any]], limit: int = 15) -> list[dict[str, Any]]:
    """Functions by self time (where samples were taken) and total time (on the stack)."""
    frames = doc["frames"]
    self_time: dict[tuple[str, str], float] = {}
    total_time: dict[tuple[str, str], float] = {}
    for path, seconds in weighted_stacks(doc, tests):
        visible = [f for f in path if frames[f][3] not in HIDDEN_KINDS] or list(path[-1:])
        leaf = frames[visible[-1]]
        key = (leaf[0], leaf[1])
        self_time[key] = self_time.get(key, 0.0) + seconds
        for function in {(frames[f][0], frames[f][1]) for f in visible}:
            total_time[function] = total_time.get(function, 0.0) + seconds
    ranked = sorted(self_time.items(), key=lambda kv: -kv[1])[:limit]
    return [
        {"path": path, "function": func, "self": round(s, 6), "total": round(total_time[(path, func)], 6)}
        for (path, func), s in ranked
    ]


def fmt(seconds: float) -> str:
    if seconds >= 60:
        return f"{seconds / 60:.1f}m"
    if seconds >= 10:
        return f"{seconds:.1f}s"
    if seconds >= 1:
        return f"{seconds:.2f}s"
    return f"{seconds * 1000:.0f}ms"
