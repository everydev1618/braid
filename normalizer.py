"""normalizer.py -- braid's "semantic gofmt", layers 0-2, on Python via stdlib `ast`.

This is the falsifiable core of braid (see DESIGN.md s.3 / s.7): if normalization cannot
fold LLM stylistic entropy down to one content hash, the whole content-addressing /
Tier-0 auto-merge / incremental-testing story collapses.

What it does:
  layer 0  parse to AST           -> whitespace, comments, redundant parens, blank lines vanish
  layer 2  scope-accurate         -> bound locals canonicalised to v0, v1, ... per a real
           alpha-rename              lexical-scope analysis; originals kept as presentation
                                     metadata (identity = structure, not names). Free names
                                     (builtins, globals, imports) are signal and preserved.

Scope analysis (so a local never captures a same-spelled free name in a sibling scope):
  - Scopes: module, function/lambda, class. Names bound in a scope = params + Store-context
    names (assign / for-target / with-as / walrus) + def/class names + comprehension targets.
  - `global`/`nonlocal` exclude a name from local binding; `import` names are bound-but-fixed
    (kept verbatim -- they carry external identity).
  - Reference resolution walks the scope chain, skipping enclosing CLASS scopes (Python rule).
    A name that resolves to a renamable binding is renamed; otherwise it is free and kept.

What it deliberately does NOT do (be a coward -- DESIGN.md s.3 "Hard rules"):
  - no operand reordering (float + is not commutative)
  - no layer-1 desugaring or layer-4 simplification yet (each rewrite must be proven
    semantics-preserving AND confluent before it earns its place)

Known limitation: comprehension targets currently bind in the enclosing function scope rather
than the comprehension's own scope (sound for hashing, slightly looser than Python 3 scoping).
"""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass


@dataclass
class Normalization:
    hash: str                 # sha256 of the canonical structure
    canonical: str            # the canonical AST dump that was hashed
    names: dict[str, str]     # canonical name -> original name (presentation metadata)


class _Scope:
    __slots__ = ("kind", "parent", "canonical", "fixed", "nonlocals")

    def __init__(self, kind: str, parent: "_Scope | None"):
        self.kind = kind                       # 'module' | 'function' | 'class'
        self.parent = parent
        self.canonical: dict[str, str] = {}    # original -> canonical (renamable)
        self.fixed: set[str] = set()           # imported: bound but kept verbatim
        self.nonlocals: set[str] = set()       # global/nonlocal-declared: not local here

    def bind(self, name: str, counter: list[int]) -> None:
        if name in self.nonlocals or name in self.fixed or name in self.canonical:
            return
        self.canonical[name] = f"v{counter[0]}"
        counter[0] += 1


def _all_args(a: ast.arguments) -> list[ast.arg]:
    res = list(a.posonlyargs) + list(a.args)
    if a.vararg:
        res.append(a.vararg)
    res += list(a.kwonlyargs)
    if a.kwarg:
        res.append(a.kwarg)
    return res


# --- Phase 1: build the scope tree and assign canonical names (pre-order DFS) ---

def _collect(node: ast.AST, scope: _Scope, smap: dict, counter: list[int]) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        scope.bind(node.name, counter)                       # def name binds in enclosing
        for d in node.args.defaults:                         # defaults: enclosing scope
            _collect(d, scope, smap, counter)
        for d in node.args.kw_defaults:
            if d:
                _collect(d, scope, smap, counter)
        for dec in node.decorator_list:
            _collect(dec, scope, smap, counter)
        child = _Scope("function", scope)
        smap[id(node)] = child
        for a in _all_args(node.args):
            child.bind(a.arg, counter)
        for a in _all_args(node.args):
            if a.annotation:
                _collect(a.annotation, scope, smap, counter)
        if node.returns:
            _collect(node.returns, scope, smap, counter)
        for stmt in node.body:
            _collect(stmt, child, smap, counter)
        return

    if isinstance(node, ast.Lambda):
        child = _Scope("function", scope)
        smap[id(node)] = child
        for a in _all_args(node.args):
            child.bind(a.arg, counter)
        _collect(node.body, child, smap, counter)
        return

    if isinstance(node, ast.ClassDef):
        scope.bind(node.name, counter)
        for dec in node.decorator_list:
            _collect(dec, scope, smap, counter)
        for base in node.bases:
            _collect(base, scope, smap, counter)
        child = _Scope("class", scope)
        smap[id(node)] = child
        for stmt in node.body:
            _collect(stmt, child, smap, counter)
        return

    if isinstance(node, (ast.Global, ast.Nonlocal)):
        for n in node.names:
            scope.nonlocals.add(n)
        return

    if isinstance(node, (ast.Import, ast.ImportFrom)):
        for al in node.names:
            scope.fixed.add(al.asname or al.name.split(".")[0])
        return

    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        scope.bind(node.id, counter)
        return

    if isinstance(node, ast.arg):
        scope.bind(node.arg, counter)
        return

    for child in ast.iter_child_nodes(node):
        _collect(child, scope, smap, counter)


# --- Reference resolution (scope chain, skipping enclosing class scopes) ---

def _resolve(name: str, scope: _Scope) -> str | None:
    cur: _Scope | None = scope
    first = True
    while cur is not None:
        if first or cur.kind != "class":
            if name in cur.canonical:
                return cur.canonical[name]
            if name in cur.fixed:
                return None                  # bound but kept verbatim (import)
        first = False
        cur = cur.parent
    return None                              # free name (builtin / unbound global)


# --- Phase 2: rename in place using the completed scope tables ---

def _transform(node: ast.AST, scope: _Scope, smap: dict) -> None:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        r = _resolve(node.name, scope)
        if r:
            node.name = r
        for d in node.args.defaults:
            _transform(d, scope, smap)
        for d in node.args.kw_defaults:
            if d:
                _transform(d, scope, smap)
        for dec in node.decorator_list:
            _transform(dec, scope, smap)
        child = smap[id(node)]
        for a in _all_args(node.args):
            r = _resolve(a.arg, child)
            if r:
                a.arg = r
            if a.annotation:
                _transform(a.annotation, scope, smap)
        if node.returns:
            _transform(node.returns, scope, smap)
        for stmt in node.body:
            _transform(stmt, child, smap)
        return

    if isinstance(node, ast.Lambda):
        child = smap[id(node)]
        for a in _all_args(node.args):
            r = _resolve(a.arg, child)
            if r:
                a.arg = r
        _transform(node.body, child, smap)
        return

    if isinstance(node, ast.ClassDef):
        r = _resolve(node.name, scope)
        if r:
            node.name = r
        for dec in node.decorator_list:
            _transform(dec, scope, smap)
        for base in node.bases:
            _transform(base, scope, smap)
        child = smap[id(node)]
        for stmt in node.body:
            _transform(stmt, child, smap)
        return

    if isinstance(node, ast.Name):
        r = _resolve(node.id, scope)
        if r:
            node.id = r
        return

    if isinstance(node, (ast.Global, ast.Nonlocal)):
        node.names = [(_resolve(n, scope) or n) for n in node.names]
        return

    for child in ast.iter_child_nodes(node):
        _transform(child, scope, smap)


def normalize(src: str) -> Normalization:
    tree = ast.parse(src)
    module = _Scope("module", None)
    smap: dict = {}
    counter = [0]

    for child in ast.iter_child_nodes(tree):
        _collect(child, module, smap, counter)
    for child in ast.iter_child_nodes(tree):
        _transform(child, module, smap)

    # ast.dump without attributes is deterministic and omits line/col info (layer 0).
    canonical = ast.dump(tree)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    names: dict[str, str] = {}
    for sc in [module, *smap.values()]:
        for orig, canon in sc.canonical.items():
            names[canon] = orig

    return Normalization(hash=digest, canonical=canonical, names=names)


def normalize_hash(src: str) -> str:
    return normalize(src).hash


# --- layer 3 building block: free (external) names of a definition ---
#
# A name referenced (Load) that does not resolve to any enclosing binding is "free":
# a builtin, a global, or a reference to a sibling top-level definition. Intersected
# with a codebase's definition names, the free set IS the dependency edge set the
# reconciler uses to decide commutativity (DESIGN.md s.4). Same scope analysis as the
# renamer -- locals and recursion (the def's own name) resolve and are excluded.

def free_names(src: str) -> set[str]:
    tree = ast.parse(src)
    module = _Scope("module", None)
    smap: dict = {}
    counter = [0]
    for child in ast.iter_child_nodes(tree):
        _collect(child, module, smap, counter)

    found: set[str] = set()

    def walk(node: ast.AST, scope: _Scope) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.args.defaults:
                walk(d, scope)
            for d in node.args.kw_defaults:
                if d:
                    walk(d, scope)
            for dec in node.decorator_list:
                walk(dec, scope)
            for a in _all_args(node.args):
                if a.annotation:
                    walk(a.annotation, scope)
            if node.returns:
                walk(node.returns, scope)
            child = smap[id(node)]
            for stmt in node.body:
                walk(stmt, child)
            return
        if isinstance(node, ast.Lambda):
            walk(node.body, smap[id(node)])
            return
        if isinstance(node, ast.ClassDef):
            for dec in node.decorator_list:
                walk(dec, scope)
            for base in node.bases:
                walk(base, scope)
            child = smap[id(node)]
            for stmt in node.body:
                walk(stmt, child)
            return
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if _resolve(node.id, scope) is None:
                found.add(node.id)
            return
        for child in ast.iter_child_nodes(node):
            walk(child, scope)

    for child in ast.iter_child_nodes(tree):
        walk(child, module)
    return found
