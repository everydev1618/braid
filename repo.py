"""repo.py -- braid as a usable thing: a `.braid/` store over a tree of source files.

A braid repo tracks one or more modules in a single language (Python or Go -- see `lang.py`).
Each top-level definition is a unit of `main`, keyed globally by `path::name` so the same name
can live in different files; the rest of each file's top level (package clause, imports,
constants) is kept verbatim as that file's preamble. Agents submit edits (a single file, or a
whole edited copy of the tree); `reconcile` folds them into main through the real engine,
records provenance, and writes each changed file back.

On-disk layout (under the repo root):
    .braid/
      config.json     {"files": [<relpath>, ...]}
      main.json       {"lang": "python"|"go",
                       "files": {relpath: {preamble, order, defs}}, "contracts": [...]}
      cells.json      provenance cells (chunks live in objects/)
      objects/<hash>  content-addressed context chunks
      sessions/<id>.json  pending edits (per-file)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import lang
from lang import run_contracts
from provenance import CellLog, Context, ContextStore, load_context
from reconciler import changeset, reconcile as reconcile_batch

PREAMBLE = "__preamble__"
SEP = "::"


def unit_key(path: str, name: str) -> str:
    return f"{path}{SEP}{name}"


def split_unit(key: str):
    path, name = key.split(SEP, 1)
    return path, name


# --- file <-> defs (dispatched to the language frontend by path) -----------

def parse_module(text: str, path: str | None = None):
    """Return (preamble, order, defs) for one module's top level."""
    return lang.for_path(path).parse_module(text)


def render_module(preamble: str, order: list, defs: dict, path: str | None = None) -> str:
    return lang.for_path(path).render_module(preamble, order, defs)


def file_state(text: str, path: str | None = None) -> dict:
    return lang.for_path(path).file_state(text)


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
            rels = _discover_src(root)
            if not rels:
                kinds = ", ".join(sorted(lang.EXTENSIONS))
                raise BraidError(f"no source files ({kinds}) found under {path}")
        elif os.path.isfile(path):
            root = os.path.dirname(path)
            rels = [os.path.relpath(path, root)]
        else:
            raise BraidError(f"no such file or directory: {path}")

        try:
            frontend = lang.detect(rels)
        except lang.UnknownLanguage as e:
            raise BraidError(str(e)) from None

        repo = cls(root)
        if os.path.isdir(repo.bdir):
            raise BraidError(f"braid repo already exists at {repo.bdir}")
        os.makedirs(repo.objects_dir, exist_ok=True)
        os.makedirs(repo.sessions_dir, exist_ok=True)

        files = {rel: file_state(_read(os.path.join(root, rel)), rel) for rel in rels}
        repo._write_json("config.json", {"files": sorted(files), "lang": frontend.name})
        repo._write_json("main.json", {"lang": frontend.name, "files": files, "contracts": []})
        repo._write_json("cells.json", [])
        return repo

    # --- main ---
    def load_main(self) -> dict:
        main = self._read_json("main.json")
        main.setdefault("lang", lang.DEFAULT.name)      # repos created before Go support
        return main

    def frontend(self):
        return lang.for_name(self.load_main()["lang"])

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
        main = self.load_main()
        main_files = main["files"]
        ext = lang.for_name(main["lang"]).ext
        path = os.path.abspath(path)
        edits, sources = {}, {}
        if os.path.isdir(path):
            for rel in _discover_src(path, (ext,)):
                text = _read(os.path.join(path, rel))
                edits[rel] = file_state(text, rel)
                sources[rel] = text
        elif os.path.isfile(path):
            rel = as_path or _match_tracked(os.path.basename(path), main_files)
            text = _read(path)
            edits[rel] = file_state(text, rel)
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
        unit = self.resolve_unit(name)
        path, defname = split_unit(unit)
        src = self.load_main()["files"][path]["defs"][defname]
        return self._load_log().provenance_of(lang.normalize_hash(unit, src))

    def context_for_hash(self, h: str):
        return self._load_log().context_for(h)

    def source_of(self, name: str) -> tuple:
        unit = self.resolve_unit(name)
        path, defname = split_unit(unit)
        return unit, self.load_main()["files"][path]["defs"][defname]

    # --- reconcile ---
    def reconcile(self, apply: bool = False, proposer=None):
        """Fold pending sessions into main. `proposer(MergeRequest) -> source | None` is the
        Tier-2 seam: on a same-definition overlap it suggests a union realization, admitted
        only if the contract gate stays green (`llm.make_merge_proposer` plugs in a model)."""
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

        res = reconcile_batch(base, sessions, base_contracts=base_contracts,
                              proposer=proposer, on_admit=on_admit)
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
            self._write_json("main.json", {"lang": main["lang"], "files": new_files,
                                           "contracts": [list(c) for c in final_contracts]})
            self._write_json("config.json", {"files": sorted(new_files), "lang": main["lang"]})
            for path, st in new_files.items():
                _write(os.path.join(self.root, path),
                       render_module(st["preamble"], st["order"], st["defs"], path))
            store.save(self.objects_dir)
            self._write_json("cells.json", log.to_list())
            for sid in admitted:
                self._remove_session(sid)

        return res, admitted, list(conflicted)

    # --- rebuild ---
    def _context_without(self, ctx: Context, unit: str) -> Context:
        """The generating context for `unit`, with `unit`'s own realization removed.

        Regeneration has to come from the *intent*. A session's context carries the whole
        edited file, which contains the answer -- hand that to a model and the "rebuild"
        proves nothing. Sibling definitions stay: those are legitimate context.
        """
        path, name = split_unit(unit)
        files = {}
        for rel, text in ctx.files.items():
            if rel != path:
                files[rel] = text
                continue
            try:
                preamble, order, defs = parse_module(text, rel)
            except (SyntaxError, ValueError):     # unparseable: hand it over untouched
                files[rel] = text
                continue
            defs.pop(name, None)
            files[rel] = render_module(preamble, [n for n in order if n != name], defs, rel)
        return Context(intent=ctx.intent, prompt=ctx.prompt, files=files,
                       messages=list(ctx.messages), model=ctx.model, params=dict(ctx.params))

    def rebuild(self, realize, apply: bool = False) -> "RebuildResult":
        """Regenerate every tracked definition from its intent and check it against the pin.

        `main.json` is the lockfile: it holds each unit's pinned realization. `realize(unit,
        context, contracts) -> source` regenerates one definition from intent alone. Each
        result is compared *by normalized hash*, so a stylistic variant counts as identical --
        matching hashes mean we regenerated **the** program, not merely *a* program. Units
        whose regeneration differs but whose contracts still pass are the residual decisions
        an intent underdetermines (DESIGN.md s.0); they are reported, never hidden.

        `apply=True` restores the working tree from the **pinned** realization, not from the
        regenerated source -- the lockfile stays authoritative, exactly as `npm ci` restores
        from the lock rather than re-resolving. The regeneration is the verification.
        """
        main = self.load_main()
        base_files = main["files"]
        contracts = [tuple(c) for c in main["contracts"]]
        log = self._load_log()

        res = RebuildResult()
        for path, name in self.list_units():
            unit = unit_key(path, name)
            pinned = base_files[path]["defs"][name]
            res.pinned[unit] = pinned
            history = log.history_of(unit)
            if not history:
                res.missing.append(unit)
                continue
            ctx = load_context(log.store, history[-1].manifest)
            regenerated = realize(unit, self._context_without(ctx, unit), contracts)
            res.rebuilt[unit] = regenerated
            if lang.normalize_hash(unit, regenerated) == lang.normalize_hash(unit, pinned):
                res.identical.append(unit)
            else:
                res.divergent.append(unit)

        codebase = {}
        for path, st in base_files.items():
            if st["preamble"].strip():                  # imports are carried, not regenerated
                codebase[unit_key(path, PREAMBLE)] = st["preamble"].rstrip() + "\n"
        codebase.update(res.rebuilt)
        for unit in res.missing:                        # nothing to regenerate from
            codebase[unit] = res.pinned[unit]
        res.failures = run_contracts(codebase, contracts)

        if apply:
            restored = dict(codebase)
            restored.update({u: res.pinned[u] for u in res.pinned})   # the lock wins
            new_files = files_from_units(restored, base_files)
            for path, st in base_files.items():
                new_files.setdefault(path, st)
            for path, st in new_files.items():
                _write(os.path.join(self.root, path),
                       render_module(st["preamble"], st["order"], st["defs"], path))
        return res

    # --- json helpers ---
    def _read_json(self, name):
        with open(os.path.join(self.bdir, name), encoding="utf-8") as f:
            return json.load(f)

    def _write_json(self, name, data):
        with open(os.path.join(self.bdir, name), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)


@dataclass
class RebuildResult:
    """Three buckets: regenerated to the same meaning, regenerated differently, or unknown."""
    identical: list = field(default_factory=list)
    divergent: list = field(default_factory=list)
    missing: list = field(default_factory=list)      # no recorded intent to rebuild from
    failures: list = field(default_factory=list)     # contract failures on the rebuilt tree
    rebuilt: dict = field(default_factory=dict)
    pinned: dict = field(default_factory=dict)

    @property
    def exact(self) -> bool:
        return not self.divergent and not self.missing

    @property
    def green(self) -> bool:
        return not self.failures


def _label(key: str) -> str:
    path, name = split_unit(key)
    return f"{path}::imports" if name == PREAMBLE else key


def _discover_src(root: str, exts=None) -> list:
    exts = tuple(exts or lang.EXTENSIONS)
    rels = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".braid" and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(exts):
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
