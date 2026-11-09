"""Tests that run what another test already runs.

- The same test collected under two node ids: a test class or function imported into another test module runs again
  there. Found from pytest's collected items.
- Parametrize cases with the same values: pytest gives them different ids, but they test the same thing. Only plain
  values (numbers, strings, None, and lists, tuples and dicts of them) are compared.
- Tests with the same code: a copied test whose name says it tests something else, usually a copy-paste mistake.
  Found from the test files' source: the bodies must be the same once the test's own local variables are renamed,
  and so must everything that could make them behave differently (decorators and parametrize data, what every name
  in them refers to, the fixtures they ask for, their class and their module's markers and setup).

Each duplicate test gets a "duplicate" entry: {"kind": "collected_twice" | "same_params" | "same_code", "of": the
node id of the test it repeats}.
"""

from __future__ import annotations

import ast
import builtins
import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

COLLECTED_TWICE = "collected_twice"
SAME_PARAMS = "same_params"
SAME_CODE = "same_code"
PLAIN_TYPES = (str, bytes, int, float, complex, bool, type(None))
MAX_SOURCE = 2_000_000  # bytes: bigger test files are generated, not copied by hand
# Module-level names that change how every test in the module runs
MODULE_SETUP = {"pytestmark", "setup_module", "teardown_module", "setUpModule", "tearDownModule", "load_tests"}
BUILTINS = set(dir(builtins))
SKELETON = re.compile(r"#[^\n]*|\w+|\s+")
Function = ast.FunctionDef | ast.AsyncFunctionDef
Definition = tuple[str, str | None, str]  # file, class, function


def collected_twice(items: list[Any]) -> dict[str, dict[str, str]]:
    """The collected pytest items that repeat another one: the same function of the same class with the same
    parameters under another node id (the original is the one in the module that defines it), or a parametrize case
    with the same values as an earlier case."""
    found: dict[str, dict[str, str]] = {}
    same: defaultdict[tuple[Any, ...], list[Any]] = defaultdict(list)
    seen_params: dict[tuple[Any, ...], str] = {}
    for item in items:
        function = getattr(item, "function", None)
        if function is None:
            continue
        function = getattr(function, "__func__", function)
        cls = getattr(item, "cls", None)
        callspec = getattr(item, "callspec", None)
        same[(id(function), id(cls), callspec.id if callspec is not None else None)].append(item)
        if callspec is None or not all(plain(value) for value in callspec.params.values()):
            continue
        params = repr(sorted(callspec.params.items()))
        marks = tuple(sorted(repr(mark) for mark in getattr(callspec, "marks", ())))
        base = item.nodeid.split("[", 1)[0]
        first = seen_params.setdefault((base, id(function), params, marks), item.nodeid)
        if first != item.nodeid:
            found[item.nodeid] = {"kind": SAME_PARAMS, "of": first}
    for group in same.values():
        if len(group) < 2:
            continue
        original = next((item for item in group if defined_in(item)), group[0])
        for item in group:
            if item is not original:
                found[item.nodeid] = {"kind": COLLECTED_TWICE, "of": original.nodeid}
    return found


def defined_in(item: Any) -> bool:
    """Whether the item's test class or function comes from the module it was collected in."""
    owner = getattr(item, "cls", None) or getattr(item, "function", None)
    module = getattr(getattr(item, "module", None), "__name__", None)
    return module is not None and getattr(owner, "__module__", None) == module


def plain(value: Any) -> bool:
    """A value whose repr says everything about it."""
    if type(value) in PLAIN_TYPES:
        return True
    if type(value) in (tuple, list, frozenset):
        return all(plain(v) for v in value)
    if type(value) is dict:
        return all(plain(k) and plain(v) for k, v in value.items())
    return False


def mark_same_code(tests: list[dict[str, Any]], rootdir: Path) -> None:
    """Mark the tests whose code is the same as an earlier test's (see the module docstring)."""
    by_definition: defaultdict[Definition, list[dict[str, Any]]] = defaultdict(list)
    for test in tests:
        if not test.get("duplicate") and not test.get("inherited_from"):
            by_definition[(test["file"], test.get("class"), test["name"].split("[", 1)[0])].append(test)
    by_file: defaultdict[str, list[tuple[str | None, str]]] = defaultdict(list)
    for file, cls, name in by_definition:
        by_file[file].append((cls, name))
    # Most tests are like no other: only those with the same punctuation (their source without names, numbers,
    # comments and whitespace) in the same directory get the full, slower comparison
    candidates: defaultdict[tuple[str, str], list[tuple[Module, Function, ast.ClassDef | None, Definition]]]
    candidates = defaultdict(list)
    for file, definitions in sorted(by_file.items()):
        module = parse_module(rootdir, file)
        if module is None:
            continue
        for cls, name in sorted(definitions, key=lambda d: (d[0] or "", d[1])):
            found = module.find(cls, name)
            if found is not None:
                function, owner = found
                candidates[(module.directory, module.skeleton(function))].append(
                    (module, function, owner, (file, cls, name))
                )
    first: dict[str, Definition] = {}
    for group in candidates.values():
        if len(group) < 2:
            continue
        for module, function, owner, definition in group:
            original = first.setdefault(module.key(function, owner), definition)
            if original != definition:
                of = by_definition[original][0]["nodeid"]
                for test in by_definition[definition]:
                    test["duplicate"] = {"kind": SAME_CODE, "of": of}


def parse_module(rootdir: Path, file: str) -> Module | None:
    path = rootdir / file
    try:
        if path.suffix != ".py" or path.stat().st_size > MAX_SOURCE:
            return None
        source = path.read_bytes()
        return Module(file, ast.parse(source, filename=str(path)), source.decode("utf-8", "replace").splitlines())
    except (OSError, SyntaxError, ValueError):
        return None


class Module:
    """A test module's source: what its names refer to, and its test functions' keys."""

    def __init__(self, file: str, tree: ast.Module, lines: list[str]) -> None:
        self.file = file
        self.lines = lines
        self.directory = str(Path(file).parent)  # its conftest.py files: their fixtures and hooks
        self.names: dict[str, str] = {}
        self.functions: dict[str, Function] = {}
        self.classes: defaultdict[str, list[ast.ClassDef]] = defaultdict(list)
        setup = []
        star = False
        for node in tree.body:
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.asname or alias.name.split(".", 1)[0]
                    self.names[name] = alias.name if alias.asname else name
            elif isinstance(node, ast.ImportFrom):
                source = "." * node.level + (node.module or "")
                if node.level:  # relative to this package
                    source = f"{self.directory}:{source}"
                for alias in node.names:
                    if alias.name == "*":
                        star = True
                    else:
                        self.names[alias.asname or alias.name] = f"{source}.{alias.name}"
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                self.names[node.name] = f"{file}:{node.name}"  # defined here: the same name elsewhere may differ
                if isinstance(node, ast.ClassDef):
                    self.add_class(node)
                else:
                    self.functions[node.name] = node
                if node.name in MODULE_SETUP or (not isinstance(node, ast.ClassDef) and autouse(node)):
                    setup.append(ast.dump(node))
            else:
                for target in assigned(node):
                    self.names[target] = f"{file}:{target}"
                    if target in MODULE_SETUP:
                        setup.append(ast.dump(node))
        # A star import leaves names unresolved: tests of this module only match each other
        self.unresolved = file if star else self.directory
        self.setup = "|".join(setup)
        self.class_keys: dict[int, str] = {}

    def add_class(self, node: ast.ClassDef) -> None:
        self.classes[node.name].append(node)
        for child in node.body:  # nested test classes
            if isinstance(child, ast.ClassDef):
                self.add_class(child)

    def find(self, cls: str | None, name: str) -> tuple[Function, ast.ClassDef | None] | None:
        """A test function and its class, unless it can't be found or does nothing."""
        owner = None
        if cls is None:
            function = self.functions.get(name)
        else:
            classes = self.classes.get(cls, [])
            if len(classes) != 1:  # not found, or two classes with that name
                return None
            owner = classes[0]
            function = next(
                (
                    node
                    for node in owner.body
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name
                ),
                None,
            )
        if function is None or empty(function):
            return None
        return function, owner

    def skeleton(self, function: Function) -> str:
        """The punctuation of a function's body: what copies share, whatever their names, numbers and comments."""
        body = function.body
        start = body[1] if len(body) > 1 and docstring(body[0]) else body[0]
        text = "\n".join(self.lines[start.lineno - 1 : function.end_lineno])
        return SKELETON.sub("", text)

    def key(self, function: Function, owner: ast.ClassDef | None) -> str:
        """What a test function has to share with another to be the same test."""
        body, free = normalize(function)
        parts = [
            self.directory,
            self.setup,
            self.class_key(owner) if owner is not None else "",
            "|".join(ast.dump(decorator) for decorator in function.decorator_list),
            ast.dump(function.args),
            body,
            "|".join(f"{n}={self.resolve(n)}" for n in sorted(free)),
            "|".join(f"{a}={self.resolve(a)}" for a in sorted(argument_names(function))),
        ]
        return hashlib.sha1("\n".join(parts).encode(), usedforsecurity=False).hexdigest()

    def class_key(self, node: ast.ClassDef) -> str:
        """Everything about a class but its tests: name, bases, decorators, setUp, class attributes, helpers."""
        key = self.class_keys.get(id(node))
        if key is None:
            parts: list[ast.AST] = [*node.bases, *node.keywords, *node.decorator_list]
            parts += [
                child
                for child in node.body
                if not (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name.startswith("test"))
            ]
            used = {n.id for part in parts for n in ast.walk(part) if isinstance(n, ast.Name)}
            names = [f"{n}={self.resolve(n)}" for n in sorted(used)]
            # Its name too: tests call the class's other tests (self.test_x()), which aren't part of this
            key = self.class_keys[id(node)] = "\n".join([node.name, *(ast.dump(part) for part in parts), *names])
        return key

    def resolve(self, name: str) -> str:
        if name in self.names:
            return self.names[name]
        if name in BUILTINS:
            return "builtins"
        return f"?{self.unresolved}"  # a fixture from a conftest.py, or a name from a star import


def autouse(node: Function) -> bool:
    return any(
        isinstance(keyword.value, ast.Constant) and keyword.arg == "autouse" and keyword.value.value is True
        for decorator in node.decorator_list
        if isinstance(decorator, ast.Call)
        for keyword in decorator.keywords
    )


def assigned(node: ast.stmt) -> list[str]:
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = node.targets
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    return [n.id for target in targets for n in ast.walk(target) if isinstance(n, ast.Name)]


def argument_names(function: Function) -> list[str]:
    args = function.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg) if a is not None]


def empty(function: Function) -> bool:
    """A test that does nothing (pass, ..., a docstring): placeholders all look the same."""
    return all(isinstance(node, ast.Pass) or docstring(node) for node in function.body)


def docstring(node: ast.stmt) -> bool:
    return isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)


def normalize(function: Function) -> tuple[str, set[str]]:
    """The function's body with its docstring dropped and its own local variables renamed in order of appearance (so
    copies that name their variables differently match), and the free names it uses. Names bound in nested functions
    and classes stay: they can be observable (a framework reading a nested function's parameter names)."""
    body = function.body[1:] if docstring(function.body[0]) else list(function.body)
    arguments = set(argument_names(function))
    declared = {
        name for node in ast.walk(function) if isinstance(node, (ast.Global, ast.Nonlocal)) for name in node.names
    }
    own = own_names(body) - arguments - declared
    bound = arguments | own
    for node in body:
        for child in ast.walk(node):  # what nested functions, classes and lambdas bind isn't free either
            if isinstance(child, ast.arg):
                bound.add(child.arg)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
                bound.add(child.id)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(child.name)
    free = {
        child.id
        for node in body
        for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load) and child.id not in bound
    }
    renamer = Renamer(own)  # renames in place: each test is compared once
    return ast.dump(ast.Module(body=[renamer.visit(node) for node in body], type_ignores=[])), free


def own_names(body: list[ast.stmt]) -> set[str]:
    """The names a function body binds in its own scope."""
    names: set[str] = set()
    stack: list[ast.AST] = list(body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        stack.extend(ast.iter_child_nodes(node))
    return names


class Renamer(ast.NodeTransformer):
    """Renames a function's own local variables, not those of the functions, classes and lambdas inside it."""

    def __init__(self, renamable: set[str]) -> None:
        self.renamable = renamable
        self.names: dict[str, str] = {}

    def rename(self, name: str) -> str:
        if name not in self.renamable:
            return name
        return self.names.setdefault(name, f"_v{len(self.names)}")

    def visit_Name(self, node: ast.Name) -> ast.Name:
        node.id = self.rename(node.id)
        return node

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> ast.ExceptHandler:
        if node.name:
            node.name = self.rename(node.name)
        self.generic_visit(node)
        return node

    def keep(self, node: ast.AST) -> ast.AST:
        return node

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = visit_Lambda = keep  # noqa: N815
