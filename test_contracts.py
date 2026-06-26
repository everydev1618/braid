"""Zero-dependency TDD spec for contract execution + gating (DESIGN.md s.4).

Run: python3 test_contracts.py

The headline (DESIGN.md s.4 "the problem that makes it a queue, not a merge"):
    green(A) and green(B)  does NOT imply  green(A union B).

Two sessions touch DISJOINT definitions (so the commutativity classifier says Tier 0
auto-merge), each satisfies every contract on its own, yet a cross-cutting contract
fails on the union. The contract gate must catch what structural commutativity cannot.
"""

import sys
import traceback

from contracts import materialize, run_contracts
from reconciler import (
    TIER0_DISJOINT,
    TIER3_CONFLICT,
    reconcile,
)


# Two disjoint config-like defs, related by a cross-cutting invariant.
AREA = {
    "width": "def width():\n    return 10\n",
    "height": "def height():\n    return 10\n",
}
CEILING = [("area_cap", "assert width() * height() <= 200")]


def test_materialize_and_run_contracts():
    assert run_contracts(AREA, CEILING) == []
    bad = {**AREA, "width": "def width():\n    return 30\n"}
    fails = run_contracts(bad, CEILING)
    assert [cid for cid, _ in fails] == ["area_cap"]


def test_materialize_exposes_definitions():
    ns = materialize(AREA)
    assert ns["width"]() == 10 and ns["height"]() == 10


def test_green_alone_red_together_rejects_second_session():
    a = {**AREA, "width": "def width():\n    return 20\n"}    # 20*10=200 -> green alone
    b = {**AREA, "height": "def height():\n    return 20\n"}  # 10*20=200 -> green alone
    res = reconcile(AREA, [("A", a, []), ("B", b, [])], base_contracts=CEILING)
    assert res.status["A"][0] == TIER0_DISJOINT              # A admitted
    assert res.status["B"][0] == TIER3_CONFLICT             # union 20*20=400 > 200
    assert res.merged["width"] == a["width"]                # A stayed in main
    assert res.merged["height"] == AREA["height"]           # B rejected, height untouched
    assert any(sid == "B" for sid, _ in res.conflicts)


def test_session_violating_ceiling_on_its_own_is_rejected():
    a = {**AREA, "width": "def width():\n    return 30\n"}   # 300 > 200 even alone
    res = reconcile(AREA, [("A", a, [])], base_contracts=CEILING)
    assert res.status["A"][0] == TIER3_CONFLICT
    assert res.merged == AREA                                # nothing admitted; main intact


def test_added_contract_is_enforced_on_later_sessions():
    # No base ceiling. A introduces the cross-cutting contract; B (disjoint def) violates it.
    sessions = [
        ("A", {**AREA, "width": "def width():\n    return 20\n"},
              [("cap", "assert width() * height() <= 200")]),
        ("B", {**AREA, "height": "def height():\n    return 20\n"}, []),
    ]
    res = reconcile(AREA, sessions)
    assert res.status["A"][0] == TIER0_DISJOINT
    assert res.status["B"][0] == TIER3_CONFLICT             # A's accumulated contract bites B


def test_disjoint_green_union_both_admit():
    a = {**AREA, "width": "def width():\n    return 15\n"}    # 15*10=150
    b = {**AREA, "height": "def height():\n    return 12\n"}  # 15*12=180 <= 200
    res = reconcile(AREA, [("A", a, []), ("B", b, [])], base_contracts=CEILING)
    assert res.status["A"][0] == TIER0_DISJOINT
    assert res.status["B"][0] == TIER0_DISJOINT
    assert not res.conflicts
    assert res.merged["width"] == a["width"]
    assert res.merged["height"] == b["height"]


def test_backward_compatible_two_tuple_sessions_still_work():
    # Sessions without contracts (2-tuples) and no base contracts behave as before.
    a = {**AREA, "width": "def width():\n    return 1\n"}
    res = reconcile(AREA, [("A", a)])
    assert res.status["A"][0] == TIER0_DISJOINT
    assert not res.conflicts


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
