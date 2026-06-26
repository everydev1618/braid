"""provenance.py -- braid's context layer (DESIGN.md requirement 1, s.2, s.5#4).

Git versions the artifact and throws away the *generating environment*. braid keeps it: every
change to a definition records a Cell linking the code (by its normalizer realization hash) to
the full context that produced it -- intent, prompt, retrieved files, conversation, model,
params.

The storage reality: sibling contexts are huge and ~90% overlapping (same retrieved files, same
conversation prefix). Storing them whole is a disk-space bomb. So a Context is shredded into
content-addressed chunks; a manifest holds chunk hashes, not bytes; and a ContextStore dedups
identical chunks. Provenance is keyed by the realization hash, so "what produced this code?" is
answered by meaning -- a stylistic variant resolves to the same provenance.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict, dataclass, field

from normalizer import normalize_hash


def _hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# --- content-addressed chunk store ---------------------------------------

class ContextStore:
    def __init__(self) -> None:
        self._blobs: dict[str, str] = {}
        self._logical_bytes = 0          # bytes presented to put() (counting duplicates)

    def put(self, content: str) -> str:
        h = _hash(content)
        self._logical_bytes += len(content.encode("utf-8"))
        self._blobs.setdefault(h, content)
        return h

    def get(self, h: str) -> str:
        return self._blobs[h]

    @property
    def num_blobs(self) -> int:
        return len(self._blobs)

    @property
    def stored_bytes(self) -> int:
        return sum(len(c.encode("utf-8")) for c in self._blobs.values())

    @property
    def logical_bytes(self) -> int:
        return self._logical_bytes

    @property
    def dedup_ratio(self) -> float:
        return self._logical_bytes / self.stored_bytes if self.stored_bytes else 1.0

    def save(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)
        for h, content in self._blobs.items():
            dest = os.path.join(path, h)
            if not os.path.exists(dest):              # content-addressed -> write-once
                with open(dest, "w", encoding="utf-8") as f:
                    f.write(content)

    @classmethod
    def load(cls, path: str) -> "ContextStore":
        store = cls()
        if os.path.isdir(path):
            for h in os.listdir(path):
                with open(os.path.join(path, h), encoding="utf-8") as f:
                    store._blobs[h] = f.read()
        return store


# --- the logical context, and its chunked manifest ------------------------

@dataclass
class Context:
    intent: str
    prompt: str
    files: dict            # path -> content (retrieved into the agent's context)
    messages: list         # conversation, oldest first
    model: str
    params: dict


@dataclass
class ContextManifest:
    intent: str            # small scalars kept inline
    model: str
    params: dict
    prompt_hash: str
    file_hashes: dict      # path -> chunk hash
    message_hashes: list   # chunk hashes, in order


def store_context(store: ContextStore, ctx: Context) -> ContextManifest:
    return ContextManifest(
        intent=ctx.intent,
        model=ctx.model,
        params=dict(ctx.params),
        prompt_hash=store.put(ctx.prompt),
        file_hashes={path: store.put(content) for path, content in ctx.files.items()},
        message_hashes=[store.put(m) for m in ctx.messages],
    )


def load_context(store: ContextStore, m: ContextManifest) -> Context:
    return Context(
        intent=m.intent,
        prompt=store.get(m.prompt_hash),
        files={path: store.get(h) for path, h in m.file_hashes.items()},
        messages=[store.get(h) for h in m.message_hashes],
        model=m.model,
        params=dict(m.params),
    )


# --- cells and the append-only log ---------------------------------------

@dataclass
class Cell:
    def_name: str
    realization_hash: str        # the normalizer's content identity -> links to the code
    parent_hash: str | None      # previous realization hash for this def
    agent: str
    seq: int                     # logical clock (monotonic; not wall time)
    manifest: ContextManifest = field(repr=False)


class CellLog:
    def __init__(self, store: ContextStore | None = None) -> None:
        self.store = store or ContextStore()
        self.cells: list[Cell] = []
        self._by_real: dict[str, Cell] = {}
        self._by_def: dict[str, list[Cell]] = {}
        self._seq = 0

    def record(self, def_name: str, source: str, context: Context, agent: str) -> Cell:
        rh = normalize_hash(source)
        prior = self._by_def.get(def_name)
        parent_hash = prior[-1].realization_hash if prior else None
        manifest = store_context(self.store, context)
        cell = Cell(def_name, rh, parent_hash, agent, self._seq, manifest)
        self._seq += 1
        self.cells.append(cell)
        self._by_real[rh] = cell                 # latest wins for a given code identity
        self._by_def.setdefault(def_name, []).append(cell)
        return cell

    def provenance_of(self, realization_hash: str) -> Cell | None:
        return self._by_real.get(realization_hash)

    def context_for(self, realization_hash: str) -> Context | None:
        cell = self._by_real.get(realization_hash)
        return load_context(self.store, cell.manifest) if cell else None

    def history_of(self, def_name: str) -> list:
        return list(self._by_def.get(def_name, []))

    # --- persistence (cells as plain dicts; chunks live in the ContextStore) ---

    def to_list(self) -> list:
        return [
            {"def_name": c.def_name, "realization_hash": c.realization_hash,
             "parent_hash": c.parent_hash, "agent": c.agent, "seq": c.seq,
             "manifest": asdict(c.manifest)}
            for c in self.cells
        ]

    @classmethod
    def from_list(cls, data: list, store: ContextStore) -> "CellLog":
        log = cls(store)
        for d in data:
            cell = Cell(d["def_name"], d["realization_hash"], d["parent_hash"],
                        d["agent"], d["seq"], ContextManifest(**d["manifest"]))
            log.cells.append(cell)
            log._by_real[cell.realization_hash] = cell
            log._by_def.setdefault(cell.def_name, []).append(cell)
            log._seq = max(log._seq, cell.seq + 1)
        return log
