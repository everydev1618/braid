"""lang.py -- the language registry: which frontend owns a file, a unit, or a codebase.

braid's model -- units keyed by meaning, normalized hashing, commutativity classification,
contract gating, provenance, rebuild -- is language-agnostic. What is language-specific is
one small surface, and this module is the whole of it:

    normalize / free_names      how a unit hashes, and what it references
    parse_module / render_module    how a file splits into units and comes back together
    run_contracts               how a composed codebase gets gated

`.py` is served by `normalizer.py` + `contracts.py` (layers 0 and 2, executable Python
contracts); `.go` by `normalizer_go.py` + `contracts_go.py` (layer 0, `go build` + `go test`).
Adding a language means adding a `Frontend` here, not touching the engine.

Dispatch is by file extension, taken from the unit key (`main.go::greeting`) or the path.
A codebase with no paths at all -- the bare `{name: source}` form the reconciler demos use --
is Python, which is what those demos have always been.

**One repo, one language.** A mixed codebase is refused rather than guessed at: contracts are
written in *a* language and there would be no sound way to decide which gate should run them.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Callable

import contracts as _py_contracts
import contracts_go as _go_contracts
import normalizer as _py
import normalizer_go as _go

SEP = "::"


@dataclass(frozen=True)
class Frontend:
    name: str
    ext: str
    normalize: Callable
    normalize_hash: Callable
    free_names: Callable
    parse_module: Callable
    render_module: Callable
    file_state: Callable
    run_contracts: Callable


PYTHON = Frontend(
    name="python", ext=".py",
    normalize=_py.normalize, normalize_hash=_py.normalize_hash, free_names=_py.free_names,
    parse_module=_py.parse_module, render_module=_py.render_module, file_state=_py.file_state,
    run_contracts=_py_contracts.run_contracts,
)

GO = Frontend(
    name="go", ext=".go",
    normalize=_go.normalize, normalize_hash=_go.normalize_hash, free_names=_go.free_names,
    parse_module=_go.parse_module, render_module=_go.render_module, file_state=_go.file_state,
    run_contracts=_go_contracts.run_contracts,
)

FRONTENDS = (PYTHON, GO)
BY_EXT = {f.ext: f for f in FRONTENDS}
BY_NAME = {f.name: f for f in FRONTENDS}
EXTENSIONS = tuple(BY_EXT)
DEFAULT = PYTHON


class UnknownLanguage(ValueError):
    pass


def for_path(path: str | None) -> Frontend:
    """The frontend for a file path. `None`/extension-less falls back to Python."""
    if not path:
        return DEFAULT
    ext = os.path.splitext(path)[1]
    if not ext:
        return DEFAULT
    if ext not in BY_EXT:
        raise UnknownLanguage(f"no braid frontend for '{ext}' files "
                              f"(supported: {', '.join(sorted(BY_EXT))})")
    return BY_EXT[ext]


def for_name(name: str | None) -> Frontend:
    if not name:
        return DEFAULT
    if name not in BY_NAME:
        raise UnknownLanguage(f"unknown language '{name}'")
    return BY_NAME[name]


def for_key(key: str) -> Frontend:
    """The frontend for a unit key (`path::name`); a bare name is Python."""
    return for_path(key.split(SEP, 1)[0]) if SEP in key else DEFAULT


def for_codebase(cb) -> Frontend:
    """The single frontend owning every unit in a codebase. Raises if they disagree."""
    found = {for_key(key) for key in cb} or {DEFAULT}
    if len(found) > 1:
        names = ", ".join(sorted(f.name for f in found))
        raise UnknownLanguage(f"mixed-language codebase ({names}): a braid repo tracks one language")
    return found.pop()


# --- the dispatching entry points the engine calls -------------------------

def normalize_hash(key: str, src: str) -> str:
    return for_key(key).normalize_hash(src)


def free_names(key: str, src: str) -> set:
    return for_key(key).free_names(src)


def run_contracts(cb, contract_list) -> list:
    """Gate a composed codebase with its language's contract runner."""
    try:
        frontend = for_codebase(cb)
    except UnknownLanguage as e:
        return [("<lang>", str(e))]
    return frontend.run_contracts(cb, contract_list)


def detect(paths) -> Frontend:
    """The frontend for a set of paths, refusing a mixture."""
    found = {for_path(p) for p in paths}
    if len(found) > 1:
        names = ", ".join(sorted(f.name for f in found))
        raise UnknownLanguage(f"{names} files in one tree: a braid repo tracks one language "
                              f"(point `braid init` at a single file, or split the tree)")
    return found.pop() if found else DEFAULT
