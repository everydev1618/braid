"""Zero-dependency TDD spec for layer-3 analysis + the minimal reconciler.

Run: python3 test_reconciler.py

The bets under test (DESIGN.md s.4):
  - A definition's dependencies = its free names intersected with the codebase.
  - Most concurrent agent work is *disjoint* (Tier 0) and auto-merges.
  - Stylistic-only edits normalize to the same hash, so they are NO-OPS -- the
    collisions git fights over simply do not exist here.
  - Same definition changed two different ways -> Tier 2, escalated (not silently merged).
  - Disjoint-but-dependency-coupled edits -> Tier 1 (auto-merge, flag for contract re-check).
"""

import sys
import traceback

from normalizer import free_names
from reconciler import (
    TIER0_DISJOINT,
    TIER1_DEP,
    TIER3_CONFLICT,
    changeset,
    reconcile,
)


BASE = {
    "f": "def f(a, b):\n    return a + b\n",
    "g": "def g(a, b):\n    return a - b\n",
}


# --- layer 3: dependency extraction ---------------------------------------

def test_free_names_are_the_dependencies():
    src = "def caller(n):\n    return helper(n) + len(n)\n"
    fn = free_names(src)
    assert "helper" in fn          # sibling def -> a real dependency
    assert "len" in fn             # builtin -> free (filtered later by codebase ∩)
    assert "n" not in fn           # param -> bound, not a dependency


def test_recursion_is_not_a_dependency():
    src = "def fact(n):\n    if n < 2:\n        return 1\n    return n * fact(n - 1)\n"
    assert "fact" not in free_names(src)   # resolves to its own def, not free


# --- reconciler: the four tiers -------------------------------------------

def test_disjoint_changes_auto_merge_tier0():
    a = {**BASE, "f": "def f(a, b):\n    return a + b + 1\n"}
    b = {**BASE, "g": "def g(a, b):\n    return a * b\n"}
    res = reconcile(BASE, [("A", a), ("B", b)])
    assert res.status["A"][0] == TIER0_DISJOINT
    assert res.status["B"][0] == TIER0_DISJOINT
    assert res.merged["f"] == a["f"]
    assert res.merged["g"] == b["g"]
    assert not res.conflicts


def test_stylistic_only_change_is_a_noop():
    # Both sessions rewrite f's surface; normalized hash is unchanged -> no change at all.
    a = {**BASE, "f": "def f(x, y):\n    return x + y\n"}
    b = {**BASE, "f": "def f(p, q):\n    return (p + q)  # same thing\n"}
    assert changeset(BASE, a).touched == set()
    res = reconcile(BASE, [("A", a), ("B", b)])
    assert not res.conflicts                 # git would conflict here; braid sees nothing


def test_same_def_conflicting_changes_tier2():
    a = {**BASE, "f": "def f(a, b):\n    return a + b + 1\n"}
    b = {**BASE, "f": "def f(a, b):\n    return a + b + 2\n"}
    res = reconcile(BASE, [("A", a), ("B", b)])
    assert res.status["A"][0] == TIER0_DISJOINT      # A lands
    assert res.status["B"][0] == TIER3_CONFLICT      # B escalates on f
    assert ("B", {"f"}) in [(sid, names) for sid, names in res.conflicts]
    assert res.merged["f"] == a["f"]                 # main stays green with A's version


def test_dependency_coupled_is_tier1():
    base = {
        "helper": "def helper(x):\n    return x * 2\n",
        "main": "def main():\n    return 0\n",
    }
    a = {**base, "helper": "def helper(x):\n    return x * 3\n"}      # change helper
    b = {**base, "caller": "def caller():\n    return helper(5)\n"}   # add a user of helper
    res = reconcile(base, [("A", a), ("B", b)])
    assert res.status["A"][0] == TIER0_DISJOINT
    assert res.status["B"][0] == TIER1_DEP           # caller depends on A's changed helper
    assert res.merged["helper"] == a["helper"]
    assert "caller" in res.merged


def test_independent_changes_to_same_module_commute():
    # Order independence: reconciling A then B == B then A for disjoint changes.
    a = {**BASE, "f": "def f(a, b):\n    return a + b + 1\n"}
    b = {**BASE, "g": "def g(a, b):\n    return a * b\n"}
    r1 = reconcile(BASE, [("A", a), ("B", b)]).merged
    r2 = reconcile(BASE, [("B", b), ("A", a)]).merged
    assert r1 == r2


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
