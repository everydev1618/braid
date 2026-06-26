"""repo.py -- braid as a usable thing: a `.braid/` store over a real Python file.

A braid repo tracks one Python module. Top-level defs (functions/classes) are the units of
`main`; anything else at top level (imports, constants) is kept verbatim as a preamble. Agents
submit candidate versions of the module as sessions; `reconcile` folds them into main through
the real engine (commutativity + contract gate + Tier-2 proposer), records provenance, and
writes the new main back to the file.

On-disk layout (next to the tracked file):
    .braid/
      config.json     {"file": <relpath>}
      main.json       {preamble, order, defs, contracts}
      cells.json      provenance cells (chunks live in objects/)
      objects/<hash>  content-addressed context chunks
      sessions/<id>.json  pending candidate changes
"""

from __future__ import annotations

import ast
import json
import os

from provenance import CellLog, Context, ContextStore
from reconciler import changeset, reconcile as reconcile_batch

_DEF_NODES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

# The module preamble (imports / top-level constants) is carried as a reserved codebase unit
# so it is materialized alongside the defs -- otherwise contracts/defs using imported names fail.
PREAMBLE = "__preamble__"


def _with_preamble(preamble: str, defs: dict) -> dict:
    cb = {}
    if preamble.strip():
        cb[PREAMBLE] = preamble.rstrip() + "\n"
    cb.update(defs)
    return cb


# --- file <-> codebase -----------------------------------------------------

def _segment(text: str, node: ast.AST) -> str:
    lines = text.splitlines()
    start = node.lineno
    for d in getattr(node, "decorator_list", []) or []:
        start = min(start, d.lineno)
    return "\n".join(lines[start - 1:node.end_lineno])


def parse_module(text: str):
    """Return (preamble, order, defs) for a Python module's top level."""
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
    for name in defs:                                   # newly added defs not in order
        if name not in seen:
            parts.append(defs[name].rstrip())
    return "\n\n\n".join(parts) + "\n"


# --- the repo --------------------------------------------------------------

class BraidError(Exception):
    pass


class BraidRepo:
    def __init__(self, root: str):
        self.root = os.path.abspath(root)
        self.bdir = os.path.join(self.root, ".braid")
        self.objects_dir = os.path.join(self.bdir, "objects")
        self.sessions_dir = os.path.join(self.bdir, "sessions")

    # locate an existing repo by walking up from cwd
    @classmethod
    def find(cls, start: str = ".") -> "BraidRepo":
        cur = os.path.abspath(start)
        while True:
            if os.path.isdir(os.path.join(cur, ".braid")):
                return cls(cur)
            parent = os.path.dirname(cur)
            if parent == cur:
                raise BraidError("not a braid repo (no .braid found); run `braid init <file.py>`")
            cur = parent

    @classmethod
    def init(cls, pyfile: str) -> "BraidRepo":
        pyfile = os.path.abspath(pyfile)
        if not os.path.isfile(pyfile):
            raise BraidError(f"no such file: {pyfile}")
        repo = cls(os.path.dirname(pyfile))
        if os.path.isdir(repo.bdir):
            raise BraidError(f"braid repo already exists at {repo.bdir}")
        os.makedirs(repo.objects_dir, exist_ok=True)
        os.makedirs(repo.sessions_dir, exist_ok=True)
        preamble, order, defs = parse_module(_read(pyfile))
        repo._write_json("config.json", {"file": os.path.relpath(pyfile, repo.root)})
        repo._save_main(preamble, order, defs, [])
        repo._write_json("cells.json", [])
        return repo

    # --- config / paths ---
    @property
    def tracked_file(self) -> str:
        return os.path.join(self.root, self._read_json("config.json")["file"])

    @property
    def tracked_basename(self) -> str:
        return os.path.basename(self.tracked_file)

    # --- main ---
    def load_main(self) -> dict:
        return self._read_json("main.json")

    def _save_main(self, preamble, order, defs, contracts):
        self._write_json("main.json", {"preamble": preamble, "order": order,
                                       "defs": defs, "contracts": contracts})

    # --- sessions ---
    def submit(self, sid: str, variant_file: str, intent: str, contracts: list, model: str = "unknown"):
        text = _read(variant_file)
        preamble, _, defs = parse_module(text)
        self._write_json(os.path.join("sessions", f"{sid}.json"), {
            "id": sid, "intent": intent, "variant": defs, "preamble": preamble,
            "contracts": [list(c) for c in contracts], "source": text, "model": model,
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

    def diff(self, sid: str) -> dict:
        """Preview a pending session against current main, by normalized meaning."""
        sessions = {s["id"]: s for s in self.load_sessions()}
        if sid not in sessions:
            raise BraidError(f"no pending session '{sid}'")
        sd = sessions[sid]
        main = self.load_main()
        base = _with_preamble(main["preamble"], main["defs"])
        variant = _with_preamble(sd.get("preamble", ""), sd["variant"])
        change = changeset(base, variant)
        items = []
        for name in sorted(change.touched):
            old, new = base.get(name), variant.get(name)
            kind = "added" if old is None else ("removed" if new is None else "modified")
            label = "imports" if name == PREAMBLE else name
            items.append({"name": label, "kind": kind, "old": old, "new": new})
        return {"intent": sd["intent"], "items": items}

    # --- provenance ---
    def _load_log(self) -> CellLog:
        return CellLog.from_list(self._read_json("cells.json"), ContextStore.load(self.objects_dir))

    def history(self, def_name: str) -> list:
        return self._load_log().history_of(def_name)

    def blame(self, def_name: str):
        main = self.load_main()
        if def_name not in main["defs"]:
            raise BraidError(f"no definition `{def_name}` in main")
        from normalizer import normalize_hash
        return self._load_log().provenance_of(normalize_hash(main["defs"][def_name]))

    def context_for_hash(self, h: str):
        return self._load_log().context_for(h)

    # --- reconcile ---
    def reconcile(self, apply: bool = False):
        main = self.load_main()
        base = _with_preamble(main["preamble"], main["defs"])
        base_contracts = [tuple(c) for c in main["contracts"]]
        sessions_data = self.load_sessions()
        if not sessions_data:
            raise BraidError("no pending sessions to reconcile (use `braid submit`)")

        by_id = {sd["id"]: sd for sd in sessions_data}
        sessions = [(sd["id"], _with_preamble(sd.get("preamble", ""), sd["variant"]),
                     [tuple(c) for c in sd["contracts"]])
                    for sd in sessions_data]

        store = ContextStore.load(self.objects_dir)
        log = CellLog.from_list(self._read_json("cells.json"), store)

        def on_admit(sid, change, current):
            sd = by_id[sid]
            ctx = Context(intent=sd["intent"], prompt=sd["intent"],
                          files={self.tracked_basename: sd["source"]}, messages=[],
                          model=sd.get("model", "unknown"), params={})
            for n in change.touched:
                if n == PREAMBLE:
                    continue
                src = current.get(n)
                if src is not None:
                    log.record(n, src, ctx, agent=sid)

        res = reconcile_batch(base, sessions, base_contracts=base_contracts, on_admit=on_admit)
        conflicted = {sid for sid, _ in res.conflicts}
        admitted = [sid for sid in res.status if sid not in conflicted]

        if apply:
            final_contracts = list(base_contracts)
            for sd in sessions_data:
                if sd["id"] in admitted:
                    final_contracts += [tuple(c) for c in sd["contracts"]]
            merged = dict(res.merged)
            new_preamble = merged.pop(PREAMBLE, "").rstrip() or main["preamble"]
            new_defs = merged
            order = [n for n in (main["order"] + [n for n in new_defs if n not in main["order"]])
                     if n in new_defs]
            self._save_main(new_preamble, order, new_defs, [list(c) for c in final_contracts])
            _write(self.tracked_file, render_module(new_preamble, order, new_defs))
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


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
