"""demo_contracts.py -- the contract gate catching what commutativity cannot.

Run: python3 demo_contracts.py

Two agents touch DIFFERENT definitions, so the structural classifier calls both Tier 0
"disjoint, auto-merge". Each change is green on its own. But a cross-cutting spec contract
fails on the UNION -- green(A) and green(B) does not imply green(A union B). The gate
catches it, keeps main green, and escalates only the genuine semantic clash.
"""

from contracts import run_contracts
from reconciler import TIER0_DISJOINT, TIER3_CONFLICT, reconcile

BASE = {
    "width": "def width():\n    return 10\n",
    "height": "def height():\n    return 10\n",
    "area": "def area():\n    return width() * height()\n",
}

# The human-authored spec ceiling -- agents cannot weaken it.
CEILING = [("area_cap", "assert area() <= 200, f'area {area()} exceeds cap'")]

SESSIONS = [
    ("agent-A: widen", {**BASE, "width": "def width():\n    return 20\n"}, []),   # 20*10=200 ok
    ("agent-B: heighten", {**BASE, "height": "def height():\n    return 20\n"}, []),  # 10*20=200 ok
]


def main() -> int:
    print("spec ceiling:", CEILING[0][1])
    print(f"base area: {BASE_area(BASE)}, within cap\n")

    print("Each session, in isolation against base:")
    for sid, variant, _ in SESSIONS:
        ok = run_contracts(variant, CEILING) == []
        print(f"  {sid:<22} -> {'GREEN' if ok else 'RED'} alone")

    print("\nReconciling both (they touch disjoint defs -> classifier says Tier 0):")
    res = reconcile(BASE, SESSIONS, base_contracts=CEILING)
    for sid, _, _ in SESSIONS:
        tier, detail = res.status[sid]
        label = {TIER0_DISJOINT: "Tier 0 admitted", TIER3_CONFLICT: "Tier 3 ESCALATED"}.get(tier, str(tier))
        print(f"  {sid:<22} -> {label}")
        if tier == TIER3_CONFLICT:
            print(f"      └─ {detail}")

    print(f"\n  main area now: {BASE_area(res.merged)} (still within the 200 cap -> main stays green)")
    print("  Lesson: structural commutativity said 'auto-merge both'. The contract gate")
    print("          caught green(A) and green(B) but red(A union B), and kept main green.")
    return 0


def BASE_area(cb):
    from contracts import materialize
    return materialize(cb)["area"]()


if __name__ == "__main__":
    raise SystemExit(main())
