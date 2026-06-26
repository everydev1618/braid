"""Zero-dependency TDD spec for the context/provenance layer (DESIGN.md req 1, s.2, s.5#4).

Run: python3 test_provenance.py

Requirement 1: keep what was in the agent's context when code was generated -- prompt,
retrieved files, conversation, model, params -- which git structurally throws away.

The storage reality (s.5#4): sibling contexts are huge and ~90% overlapping, so they MUST be
content-addressed and chunk-deduplicated or it is a disk-space bomb. And provenance is keyed by
the *realization hash* (the normalizer's content identity), so the question "what produced this
code?" is answered by meaning, not by surface text.
"""

import sys
import traceback

from normalizer import normalize_hash
from provenance import CellLog, Context, ContextStore, load_context, store_context
from reconciler import reconcile


def _ctx(prompt, files=None, messages=None, intent="do a thing", model="claude-opus-4-8"):
    return Context(intent=intent, prompt=prompt, files=files or {},
                   messages=messages or [], model=model, params={"temperature": 0})


# --- chunk store + dedup --------------------------------------------------

def test_store_dedups_identical_chunks():
    s = ContextStore()
    h1 = s.put("hello")
    h2 = s.put("hello")
    h3 = s.put("world")
    assert h1 == h2 and h1 != h3
    assert s.get(h1) == "hello"
    assert s.num_blobs == 2                 # "hello" stored once
    assert s.logical_bytes > s.stored_bytes  # dedup actually saved space


def test_context_roundtrip():
    s = ContextStore()
    ctx = _ctx("write a parser", files={"a.py": "AAA", "b.py": "BBB"},
               messages=["hi", "ok"])
    manifest = store_context(s, ctx)
    assert load_context(s, manifest) == ctx


def test_overlapping_sibling_contexts_dedup_hard():
    # 12 agents, each with the same big retrieved files + shared conversation prefix,
    # differing only in a small unique prompt. Storage must be ~unique-content, not 12x.
    s = ContextStore()
    shared_files = {"util.py": "U" * 5000, "config.py": "C" * 5000}
    shared_convo = ["S" * 3000, "T" * 3000]
    for i in range(12):
        store_context(s, _ctx(f"unique prompt {i}", files=shared_files, messages=shared_convo))
    # 12 * (10000 files + 6000 convo + ~tiny prompt) logical...
    assert s.logical_bytes > 190_000
    # ...but the shared 16000 is stored once; only the 12 tiny prompts are unique.
    assert s.stored_bytes < 20_000
    assert s.dedup_ratio > 9.0


# --- cells: linking realization hash -> generating context ----------------

def test_provenance_links_code_hash_to_context():
    log = CellLog()
    src = "def f(a, b):\n    return a + b\n"
    log.record("f", src, _ctx("make f add", intent="add two numbers"), agent="agent-1")
    cell = log.provenance_of(normalize_hash(src))
    assert cell is not None and cell.agent == "agent-1"
    ctx = log.context_for(normalize_hash(src))
    assert ctx.intent == "add two numbers"
    assert ctx.prompt == "make f add"


def test_provenance_is_keyed_by_meaning_not_surface():
    # A stylistic variant normalizes to the same hash, so it resolves to the same provenance.
    log = CellLog()
    log.record("f", "def f(a, b):\n    return a + b\n", _ctx("p"), agent="agent-1")
    variant = "def f(x, y):\n    return (x + y)  # reformatted\n"
    assert log.provenance_of(normalize_hash(variant)) is not None


def test_history_of_a_definition_is_ordered():
    log = CellLog()
    log.record("f", "def f():\n    return 1\n", _ctx("v1"), agent="a1")
    log.record("f", "def f():\n    return 2\n", _ctx("v2"), agent="a2")
    hist = log.history_of("f")
    assert [c.agent for c in hist] == ["a1", "a2"]
    assert hist[1].parent_hash == hist[0].realization_hash   # chained


def test_shared_store_across_cells_dedups():
    # Two cells generated with overlapping context share blobs in one store.
    log = CellLog()
    big = {"shared.py": "Z" * 8000}
    log.record("f", "def f():\n    return 1\n", _ctx("p1", files=big), agent="a1")
    log.record("g", "def g():\n    return 2\n", _ctx("p2", files=big), agent="a2")
    assert log.store.dedup_ratio > 1.5      # the 8000-byte file stored once, not twice


def test_reconcile_records_provenance_only_for_admitted_work():
    base = {"f": "def f():\n    return 0\n"}
    a = {"f": "def f():\n    return 1\n"}
    b = {"f": "def f():\n    return 2\n"}        # conflicts with A on f -> escalated, no proposer
    contexts = {"A": _ctx("make f return 1"), "B": _ctx("make f return 2")}
    log = CellLog()

    def on_admit(sid, change, current):
        for name in change.touched:
            log.record(name, current[name], contexts[sid], agent=sid)

    res = reconcile(base, [("A", a), ("B", b)], on_admit=on_admit)
    # A admitted, B escalated -> only A's provenance recorded.
    assert log.provenance_of(normalize_hash(a["f"])) is not None
    assert log.provenance_of(normalize_hash(b["f"])) is None
    assert res.merged["f"] == a["f"]
    # And from main's code we can recover what produced it:
    assert log.context_for(normalize_hash(res.merged["f"])).prompt == "make f return 1"


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
