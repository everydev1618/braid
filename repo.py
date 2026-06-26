"""repo.py -- braid as a usable thing: a `.braid/` store over a tree of Python files.

A braid repo tracks one or more Python modules. Each top-level def (function/class) is a unit
of `main`, keyed globally by `path::name` so the same name can live in different files; the rest
of each file's top level (imports, constants) is kept verbatim as that file's preamble. Agents
submit edits (a single file, or a whole edited copy of the tree); `reconcile` folds them into
main through the real engine, records provenance, and writes each changed file back.

On-disk layout (under the repo root):
    .braid/
      config.json     {"files": [<relpath>, ...]}
      main.json       {"files": {relpath: {preamble, order, defs}}, "contracts": [...]}
      cells.json      provenance cells (chunks live in objects/)
      objects/<hash>  content-addressed context chunks
      sessions/<id>.json  pending edits (per-file)
"""

from __future__ import annotations

import ast
import json
import os

from provenance import CellLog, Context, ContextStore
from reconciler import changeset, reconcile as reconcile_batch

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
PREAMBLE = "__preamble__"
SEP = "::"


def unit_key(path: str, name: str) -> str:
    return f"{path}{SEP}{name}"


def split_unit(key: str):
    path, name = key.split(SEP, 1)
    return path, name


# --- file <-> defs ---------------------------------------------------------

def _segment(text: str, node: ast.AST) -> str:
    lines = text.splitlines()
    start = node.lineno
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, d.lineno)
    return "\n".join(lines[start - 1:node.end_lineno])


def parse_module(text: str):
    """Return (preamble, order, defs) for one module's top level."""
    tree = ast.parse(text)
    preamble_parts, order, defs = [], [], {}
    for node in tree.body:
        if isinstance(node, _DEF_NODES):
            defs[node.name] = _segment(text, node).rstrip() + "\n"
            order.append(node.name)
        else:
            seg = _segment(text, node)
            if seg.strip():
                preamble_parts.append(seg.rstrip())
    return "\n".join(preamble_parts), order, defs


def render_module(preamble: str, order: list, defs: dict) -> str:
    parts = []
    if preamble.strip():
        parts.append(preamble.rstrip())
    seen = set()
    for name in order:
        if name in defs:
            parts.append(defs[name].rstrip())
            seen.add(name)
    for name in defs:
        if name not in seen:
            parts.append(defs[name].rstrip())
    return "\n\n\n".join(parts) + "\n"


def file_state(text: str) -> dict:
    preamble, order, defs = parse_module(text)
    return {"preamble": preamble, "order": order, "defs": defs}


# --- files <-> units (the reconciler's codebase view) ----------------------

def units_from_files(files: dict) -> dict:
    cb = {}
    for path, st in files.items():
        if st["preamble"].strip():
            cb[unit_key(path, PREAMBLE)] = st["preamble"].rstrip() + "\n"
        for name, src in st["defs"].items():
            cb[unit_key(path, name)] = src
    return cb


def files_from_units(units: dict, prev_files: dict) -> dict:
    files: dict = {}
    for key, src in units.items():
        path, name = split_unit(key)
        f = files.setdefault(path, {"preamble": "", "order": [], "defs": {}})
        if name == PREAMBLE:
            f["preamble"] = src.rstrip()
        else:
            f["defs"][name] = src
    for path, f in files.items():
        prev_order = prev_files.get(path, {}).get("order", [])
        f["order"] = ([n for n in prev_order if n in f["defs"]]
                      + [n for n in f["defs"] if n not in prev_order])
    return files


# --- the repo --------------------------------------------------------------

class BraidError(Exception):
    pass


class BraidRepo:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.bdir = os.path.join(self.root, ".braid")
        self.objects_dir = os.path.join(self.bdir, "objects")
        self.sessions_dir = os.path.join(self.bdir, "sessions")

    @classmethod
    def find(cls, start: str = ".") -> "BraidRepo":
        cur = os.path.abspath(start)
        while True:
            if os.path.isdir(os.path.join(cur, ".braid")):
                return cls(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                raise BraidError("not a braid repo (no .braid found); run `braid init <path>`")
            cur = parent

    @classmethod
    def init(cls, path: str) -> "BraidRepo":
        path = os.path.abspath(path)
        if os.path.isdir(path):
            root = path
            rels = _discover_py(root)
            if not rels:
                raise BraidError(f"no .py files found under {path}")
        elif os.path.isfile(path):
            root = os.path.dirname(path)
            rels = [os.path.relpath(path, root)]
        else:
            raise BraidError(f"no such file or directory: {path}")

        repo = cls(root)
        if os.path.isdir(repo.bdir):
            raise BraidError(f"braid repo already exists at {repo.bdir}")
        os.makedirs(repo.objects_dir, exist_ok=True)
        os.makedirs(repo.sessions_dir, exist_ok=True)

        files = {rel: file_state(_read(os.path.join(root, rel))) for rel in rels}
        repo._write_json("config.json", {"files": sorted(files)})
        repo._write_json("main.json", {"files": files, "contracts": []})
        repo._write_json("cells.json", [])
        return repo

    # --- main ---
    def load_main(self) -> dict:
        return self._read_json("main.json")

    def tracked_files(self) -> list:
        return sorted(self.load_main()["files"])

    def list_units(self) -> list:
        out = []
        for path, st in sorted(self.load_main()["files"].items()):
            out += [(path, name) for name in st["order"]]
        return out

    # --- sessions ---
    def submit(self, sid: str, path: str, intent: str, contracts: list,
               model: str = "unknown", as_path: str | None = None):
        main_files = self.load_main()["files"]
        path = os.path.abspath(path)
        edits, sources = {}, {}
        if os.path.isdir(path):
            for rel in _discover_py(path):
                text = _read(os.path.join(path, rel))
                edits[rel] = file_state(text)
                sources[rel] = text
        elif os.path.isfile(path):
            rel = as_path or _match_tracked(os.path.basename(path), main_files)
            text = _read(path)
            edits[rel] = file_state(text)
            sources[rel] = text
        else:
            raise BraidError(f"no such file or directory: {path}")

        self._write_json(os.path.join("sessions", f"{sid}.json"), {
            "id": sid, "intent": intent, "edits": edits, "sources": sources,
            "contracts": [list(c) for c in contracts], "model": model,
        })

    def load_sessions(self) -> list:
        if not os.path.isdir(self.sessions_dir):
            return []
        out = []
        for fn in sorted(os.listdir(self.sessions_dir)):
            if fn.endswith(".json"):
                with open(os.path.join(self.sessions_dir, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
        return out

    def _remove_session(self, sid: str):
        p = os.path.join(self.sessions_dir, f"{sid}.json")
        if os.path.exists(p):
            os.remove(p)

    def abandon(self, sid: str):
        p = os.path.join(self.sessions_dir, f"{sid}.json")
        if not os.path.exists(p):
            raise BraidError(f"no pending session '{sid}'")
        os.remove(p)

    def _variant_units(self, base_files: dict, edits: dict) -> dict:
        merged = dict(base_files)
        merged.update(edits)                    # whole-file overlay; unedited files unchanged
        return units_from_files(merged)

    def diff(self, sid: str) -> dict:
        sessions = {s["id"]: s for s in self.load_sessions()}
        if sid not in sessions:
            raise BraidError(f"no pending session '{sid}'")
        sd = sessions[sid]
        base_files = self.load_main()["files"]
        base = units_from_files(base_files)
        variant = self._variant_units(base_files, sd["edits"])
        change = changeset(base, variant)
        items = []
        for key in sorted(change.touched):
            old, new = base.get(key), variant.get(key)
            kind = "added" if old is None else ("removed" if new is None else "modified")
            items.append({"name": _label(key), "kind": kind, "old": old, "new": new})
        return {"intent": sd["intent"], "items": items}

    # --- provenance ---
    def _load_log(self) -> CellLog:
        return CellLog.from_list(self._read_json("cells.json"), ContextStore.load(self.objects_dir))

    def history(self, unit: str) -> list:
        return self._load_log().history_of(unit)

    def resolve_unit(self, name: str) -> str:
        if SEP in name:
            return name
        matches = [unit_key(p, n) for p, n in self.list_units() if n == name]
        if not matches:
            raise BraidError(f"no definition `{name}` in main")
        if len(matches) > 1:
            raise BraidError(f"`{name}` is ambiguous: {', '.join(matches)} (use path::name)")
        return matches[0]

    def blame(self, name: str):
        from normalizer import normalize_hash
        unit = self.resolve_unit(name)
        path, defname = split_unit(unit)
        src = self.load_main()["files"][path]["defs"][defname]
        return self._load_log().provenance_of(normalize_hash(src))

    def context_for_hash(self, h: str):
        return self._load_log().context_for(h)

    def source_of(self, name: str) -> tuple:
        unit = self.resolve_unit(name)
        path, defname = split_unit(unit)
        return unit, self.load_main()["files"][path]["defs"][defname]

    # --- reconcile ---
    def reconcile(self, apply: bool = False):
        main = self.load_main()
        base_files = main["files"]
        base = units_from_files(base_files)
        base_contracts = [tuple(c) for c in main["contracts"]]
        sessions_data = self.load_sessions()
        if not sessions_data:
            raise BraidError("no pending sessions to reconcile (use `braid submit`)")

        by_id = {sd["id"]: sd for sd in sessions_data}
        sessions = [(sd["id"], self._variant_units(base_files, sd["edits"]),
                     [tuple(c) for c in sd["contracts"]]) for sd in sessions_data]

        store = ContextStore.load(self.objects_dir)
        log = CellLog.from_list(self._read_json("cells.json"), store)

        def on_admit(sid, change, current):
            sd = by_id[sid]
            ctx = Context(intent=sd["intent"], prompt=sd["intent"], files=dict(sd["sources"]),
                          messages=[], model=sd.get("model", "unknown"), params={})
            for key in change.touched:
                if key.endswith(SEP + PREAMBLE):
                    continue
                src = current.get(key)
                if src is not None:
                    log.record(key, src, ctx, agent=sid)

        res = reconcile_batch(base, sessions, base_contracts=base_contracts, on_admit=on_admit)
        conflicted = {sid for sid, _ in res.conflicts}
        admitted = [sid for sid in res.status if sid not in conflicted]

        if apply:
            final_contracts = list(base_contracts)
            for sd in sessions_data:
                if sd["id"] in admitted:
                    final_contracts += [tuple(c) for c in sd["contracts"]]
            new_files = files_from_units(res.merged, base_files)
            # carry over any tracked files that had no units at all (e.g. empty modules)
            for path, st in base_files.items():
                new_files.setdefault(path, st)
            self._write_json("main.json", {"files": new_files,
                                           "contracts": [list(c) for c in final_contracts]})
            self._write_json("config.json", {"files": sorted(new_files)})
            for path, st in new_files.items():
                _write(os.path.join(self.root, path),
                       render_module(st["preamble"], st["order"], st["defs"]))
            store.save(self.objects_dir)
            self._write_json("cells.json", log.to_list())
            for sid in admitted:
                self._remove_session(sid)

        return res, admitted, list(conflicted)

    # --- json helpers ---
    def _read_json(self, name):
        with open(os.path.join(self.bdir, name), encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, name, data):
        with open(os.path.join(self.bdir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


def _label(key: str) -> str:
    path, name = split_unit(key)
    return f"{path}::imports" if name == PREAMBLE else key


def _discover_py(root: str) -> list:
    rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".braid" and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                rels.append(os.path.relpath(os.path.join(dirpath, fn), root))
    return sorted(rels)


def _match_tracked(basename: str, main_files: dict) -> str:
    matches = [rel for rel in main_files if os.path.basename(rel) == basename]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        if len(main_files) == 1:
            return next(iter(main_files))
        raise BraidError(f"can't map {basename} to a tracked file; pass --as <relpath>")
    raise BraidError(f"{basename} matches several tracked files; pass --as <relpath>")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
