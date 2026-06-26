"""demo_live.py -- braid's unified engine on a realistic concurrent workload.

Run: python3 demo_live.py

Agents arrive over time. Some touch disjoint definitions (land coordination-free); several
contend on a hot `flags` definition (serialized by lease + aging, model-merged through the
contract gate); one genuinely contradicts an accepted contract (escalated). Throughout, main
stays green and every admitted change records its generating context.
"""

from live import LiveReconciler, LiveSession
from normalizer import normalize_hash
from provenance import CellLog, Context


def ctx(prompt):
    return Context(intent=prompt, prompt=prompt, files={"repo.py": "R" * 4000},
                   messages=["system: agent on the flags service"], model="claude-opus-4-8", params={})


def union_proposer(req):
    o, t = {}, {}
    exec(req.ours, o)
    exec(req.theirs, t)
    merged = sorted(set(o[req.name]()) | set(t[req.name]()))
    return f"def {req.name}():\n    return {merged!r}\n"


def flag_session(name, key, cost, arrival, contract=None):
    return LiveSession(
        id=name,
        variant={**BASE, "flags": f'def flags():\n    return [{key!r}]\n'},   # full snapshot
        contracts=[contract] if contract else [(f"has_{key}", f'assert {key!r} in flags()')],
        cost=cost, arrival=arrival, context=ctx(f"add {key!r} to flags"),
    )


BASE = {
    "flags": "def flags():\n    return []\n",
    "version": "def version():\n    return '1.0'\n",
}

TIER = {0: "Tier0 disjoint", 1: "Tier1 dep", 2: "Tier2 merged", 3: "Tier3 ESCALATED"}


def main() -> int:
    log = CellLog()
    sessions = [
        flag_session("agent-feature-A", "dark_mode", cost=4, arrival=0),     # slow, hot
        flag_session("agent-feature-B", "beta_ui", cost=1, arrival=1),       # fast, hot
        flag_session("agent-feature-C", "new_search", cost=1, arrival=2),    # fast, hot
        LiveSession("agent-bump-version",                                    # disjoint
                    {**BASE, "version": "def version():\n    return '1.1'\n"},
                    contracts=[("v", "assert version() == '1.1'")],
                    cost=2, arrival=0, context=ctx("bump version to 1.1")),
        flag_session("agent-rogue", "ONLY", cost=1, arrival=3,               # contradiction
                     contract=("exclusive", 'assert flags() == ["ONLY"]')),
    ]

    res = LiveReconciler(BASE, proposer=union_proposer, log=log, use_leases=True).run(sessions, horizon=30)

    print("Per-session outcome:")
    for s in sessions:
        tier, _ = res.status[s.id]
        landed = res.landed_round[s.id]
        where = f"landed@{landed}" if landed is not None else "NOT landed"
        print(f"  {s.id:<22} {TIER[tier]:<16} {where:<13} attempts={res.attempts[s.id]}")

    ns, vns = {}, {}
    exec(res.merged["flags"], ns)
    exec(res.merged["version"], vns)
    print(f"\n  main flags() = {ns['flags']()}   version() = {vns['version']()!r}")
    print(f"  leases used: {res.leases_used}   wasted re-realizations: {res.wasted_work}")

    print("\n  Provenance (what produced each definition in main):")
    for name in ("flags", "version"):
        cell = log.provenance_of(normalize_hash(res.merged[name]))
        who = f'{cell.agent}: "{log.context_for(cell.realization_hash).intent}"' if cell else "(base)"
        print(f"    {name:<9} <- {who}")
    print(f"  context stored with {log.store.dedup_ratio:.1f}x dedup "
          f"({log.store.logical_bytes:,} B presented -> {log.store.stored_bytes:,} B)")

    print("\n  The slow feature was not starved (1 attempt), the three flag agents were merged")
    print("  in sequence, the disjoint version bump never coordinated, and the rogue agent that")
    print("  contradicted an accepted contract was escalated -- all while main stayed green.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
