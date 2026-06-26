"""demo.py -- braid reconciling six concurrent agent sessions on one codebase.

Run: python3 demo.py

Shows the central bet (DESIGN.md s.4): most concurrent agent work is disjoint (Tier 0) and
auto-merges; dependency-coupled work auto-merges with a flag (Tier 1); stylistic-only edits
that git would fight over are no-ops here; only a genuine same-definition disagreement
(Tier 3) reaches a human -- and main stays green meanwhile.
"""

from reconciler import (
    TIER0_DISJOINT,
    TIER1_DEP,
    TIER3_CONFLICT,
    changeset,
    reconcile,
)

TIER_LABEL = {
    TIER0_DISJOINT: "Tier 0  disjoint        -> auto-merge",
    TIER1_DEP:      "Tier 1  dep-coupled     -> auto-merge + flag",
    TIER3_CONFLICT: "Tier 3  CONFLICT        -> escalate to human",
}

BASE = {
    "add":   "def add(a, b):\n    return a + b\n",
    "sub":   "def sub(a, b):\n    return a - b\n",
    "scale": "def scale(x, k):\n    return x * k\n",
    "clamp": "def clamp(v, lo, hi):\n    return scale(v, 1)\n",   # uses scale
}

# Six agents working the codebase at the same time.
SESSIONS = [
    # 1. disjoint behavioural change to `sub`
    ("agent-1: fix sub",
     {**BASE, "sub": "def sub(a, b):\n    return a - b if a > b else 0\n"}),

    # 2. new helper that calls `scale` -> will couple with agent-4's scale change
    ("agent-2: add double",
     {**BASE, "double": "def double(n):\n    return scale(n, 2)\n"}),

    # 3. pure restyle of `add` (rename + parens) -- semantically identical
    ("agent-3: restyle add",
     {**BASE, "add": "def add(left, right):\n    return (left + right)  # tidy\n"}),

    # 4. changes `scale`, which `clamp` depends on -> dependency-coupled
    ("agent-4: scale rounds",
     {**BASE, "scale": "def scale(x, k):\n    return round(x * k)\n"}),

    # 5. ANOTHER restyle of `add` -- in git this collides with agent-3; here both are no-ops
    ("agent-5: restyle add again",
     {**BASE, "add": "def add(p, q):\n    return p + q\n"}),

    # 6. genuinely disagrees with agent-1 about what `sub` should do
    ("agent-6: clamp sub differently",
     {**BASE, "sub": "def sub(a, b):\n    return max(a - b, -1)\n"}),
]


def main() -> int:
    print("BASE codebase:", ", ".join(sorted(BASE)), "\n")
    res = reconcile(BASE, SESSIONS)

    for sid, _ in SESSIONS:
        tier, detail = res.status[sid]
        touched = sorted(changeset(BASE, dict(SESSIONS)[sid]).touched) or ["(nothing)"]
        print(f"  {sid:<32} touches {','.join(touched):<10} {TIER_LABEL[tier]}")
        if detail.startswith("conflicts") or "flag" in TIER_LABEL[tier]:
            print(f"      └─ {detail}")

    print("\n  RESULT: main =", ", ".join(sorted(res.merged)))
    auto = sum(1 for t, _ in res.status.values() if t in (TIER0_DISJOINT, TIER1_DEP))
    print(f"  {auto}/{len(SESSIONS)} sessions integrated automatically, "
          f"{len(res.conflicts)} escalated to a human.")
    if res.conflicts:
        for sid, names in res.conflicts:
            print(f"    conflict: {sid} disagrees on {sorted(names)} "
                  f"(main kept the already-green version)")
    print("\n  Note: agent-3 and agent-5 both rewrote `add`; git would have conflicted.")
    print("        braid normalized both to the existing hash -> zero changes, zero conflict.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
