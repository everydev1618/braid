"""demo_merge.py -- Tier 2: the model proposes, the contract disposes.

Run: python3 demo_merge.py

Two agents edit the SAME function `greet` in incompatible ways -- in git, a merge conflict a
human resolves by hand. braid hands it to a model, which proposes a combined version. The
proposal is admitted only if it passes BOTH agents' contracts. We run it twice: once with a
competent model (auto-merged, no human) and once with a careless model (rejected by the gate,
escalated) -- showing the gate, not the model, is the authority.
"""

from contracts import materialize
from reconciler import TIER2_MERGED, TIER3_CONFLICT, reconcile

BASE = {"greet": 'def greet(name):\n    return "hi " + name\n'}
SESSIONS = [
    ("agent-A: add !", {"greet": 'def greet(name):\n    return "hi " + name + "!"\n'},
     [("excl", 'assert greet("bob").endswith("!")')]),
    ("agent-B: capitalize", {"greet": 'def greet(name):\n    return "hi " + name.capitalize()\n'},
     [("cap", 'assert "Bob" in greet("bob")')]),
]

GOOD = 'def greet(name):\n    return "hi " + name.capitalize() + "!"\n'   # honors both intents
LAZY = 'def greet(name):\n    return "hi " + name.capitalize()\n'         # forgets the "!"


def run(label, model_reply):
    res = reconcile(BASE, SESSIONS, proposer=lambda req: model_reply)
    tier = res.status["agent-B: capitalize"][0]
    outcome = {TIER2_MERGED: "AUTO-MERGED (no human)", TIER3_CONFLICT: "ESCALATED to human"}[tier]
    print(f"\n  {label}:")
    print(f"    agent-B -> {outcome}")
    print(f"    main greet('bob') = {materialize(res.merged)['greet']('bob')!r}")
    if res.conflicts:
        print(f"    (gate rejected the proposal; main kept agent-A's green version)")


def main() -> int:
    print("Both agents changed `greet` -- a classic same-file merge conflict.")
    run("competent model proposes 'hi ' + name.capitalize() + '!'", GOOD)
    run("careless model proposes a version missing the '!'", LAZY)
    print("\n  The model never decides -- the contract gate does. Same conflict, two models,")
    print("  two outcomes: a good proposal merges with no human; a bad one is escalated.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
