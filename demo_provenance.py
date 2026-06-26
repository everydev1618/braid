"""demo_provenance.py -- braid keeps the context git throws away (requirement 1).

Run: python3 demo_provenance.py

Four agents reconcile changes; each carries the context that generated its code (intent,
prompt, retrieved files, conversation, model). braid records a Cell per admitted change linking
the code's realization hash to that context, with all the big overlapping chunks deduplicated.
Afterwards we point at a line of `main` and ask: who/what produced this, and why?
"""

from normalizer import normalize_hash
from provenance import CellLog, Context
from reconciler import reconcile

# A large repo snapshot that every agent had in context (the ~90%-shared part).
REPO_SNAPSHOT = {
    "models.py": "# 4 KB of shared model code\n" + "M" * 4000,
    "db.py": "# 3 KB of shared db code\n" + "D" * 3000,
}
SHARED_CONVO = ["system: you are a coding agent on the billing service", "user: improve billing"]

BASE = {
    "tax": "def tax(amount):\n    return amount * 0.1\n",
    "total": "def total(amount):\n    return amount + tax(amount)\n",
    "fee": "def fee(amount):\n    return 0\n",
}

SESSIONS = [
    ("agent-1", {**BASE, "tax": "def tax(amount):\n    return amount * 0.2\n"},
     "raise the tax rate to 20% for the new jurisdiction"),
    ("agent-2", {**BASE, "fee": "def fee(amount):\n    return 2 if amount > 100 else 0\n"},
     "add a flat $2 handling fee on orders over $100"),
    ("agent-3", {**BASE, "discount": "def discount(amount):\n    return amount * 0.05\n"},
     "introduce a 5% loyalty discount helper"),
]


def ctx_for(prompt):
    return Context(intent=prompt, prompt=prompt, files=REPO_SNAPSHOT,
                   messages=SHARED_CONVO, model="claude-opus-4-8", params={"temperature": 0})


def main() -> int:
    log = CellLog()
    contexts = {sid: ctx_for(prompt) for sid, _, prompt in SESSIONS}

    def on_admit(sid, change, current):
        for name in change.touched:
            log.record(name, current[name], contexts[sid], agent=sid)

    sessions = [(sid, variant) for sid, variant, _ in SESSIONS]
    res = reconcile(BASE, sessions, on_admit=on_admit)

    print("main now:", ", ".join(sorted(res.merged)))
    print(f"cells recorded: {len(log.cells)}\n")

    print("Provenance of each definition currently in main:")
    for name in sorted(res.merged):
        cell = log.provenance_of(normalize_hash(res.merged[name]))
        if cell:
            print(f"  {name:<10} <- {cell.agent}: \"{log.context_for(cell.realization_hash).intent}\"")
        else:
            print(f"  {name:<10} <- (from base, no agent provenance)")

    store = log.store
    naive = store.logical_bytes
    actual = store.stored_bytes
    print(f"\nContext storage: {naive:,} B of context presented -> {actual:,} B stored "
          f"({store.dedup_ratio:.1f}x dedup)")
    print("  The 7 KB repo snapshot + shared conversation each agent carried is stored ONCE,")
    print("  not once per cell -- the disk-space bomb (DESIGN.md s.5#4) defused.")
    print("\nThis is what git cannot do: from a line in main, recover the prompt, files,")
    print("conversation, and model that generated it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
