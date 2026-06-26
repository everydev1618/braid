"""live.py -- the unified engine: scheduling + leasing + contract-gated integration + provenance.

The batch `reconcile` folds sessions in list order with no notion of time. Real agents arrive
over time, take work to realize, and contend on a moving `main`. `LiveReconciler` drives that:

  - sessions arrive at a round and take `cost` rounds to realize;
  - DISJOINT (non-contended) sessions start immediately and land coordination-free;
  - CONTENDED (hot-def) sessions need a per-def lease granted by aging, which freezes the def
    so the holder realizes against a stable target and lands in one attempt (no livelock);
  - every landing goes through the real `integrate` (structural/contract gate + proposer), so
    `main` stays green and same-def overlaps are model-merged or escalated;
  - admitted work records provenance via the CellLog.

Set `use_leases=False` to get the optimistic (no-coordination) policy, which starves a slow
session on a hot def -- the contrast `demo_live.py` shows. All deterministic (no wall clock).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reconciler import changeset, integrate, IntegrationState


@dataclass
class LiveSession:
    id: str
    variant: dict
    contracts: tuple = ()
    arrival: int = 0
    cost: int = 1
    context: object = None        # provenance.Context, optional


@dataclass
class LiveResult:
    merged: dict
    status: dict                  # id -> (tier, detail)
    conflicts: list               # [(id, names)]
    landed_round: dict            # id -> round, or None if never landed
    attempts: dict                # id -> realize attempts (>1 means wasted re-realization)
    leases_used: int

    @property
    def all_landed(self) -> bool:
        return all(r is not None for r in self.landed_round.values())

    @property
    def wasted_work(self) -> int:
        return sum(a - 1 for a in self.attempts.values())


class LiveReconciler:
    def __init__(self, base, base_contracts=(), proposer=None, log=None,
                 use_leases=True, hot_threshold=2):
        self.base = dict(base)
        self.base_contracts = list(base_contracts)
        self.proposer = proposer
        self.log = log
        self.use_leases = use_leases
        self.hot_threshold = hot_threshold

    def run(self, sessions: list, horizon: int) -> LiveResult:
        state = IntegrationState.start(self.base, self.base_contracts)
        targets = {s.id: changeset(state.base, s.variant).touched for s in sessions}

        hot: set = set()
        if self.use_leases:
            count: dict = {}
            for s in sessions:
                for n in targets[s.id]:
                    count[n] = count.get(n, 0) + 1
            hot = {n for n, c in count.items() if c >= self.hot_threshold}

        rt = {s.id: dict(started=False, remaining=0, start_ver=-1, attempts=0,
                         has_lease=False, waited=0, landed=None, done=False) for s in sessions}
        version = 0
        def_version: dict = {}
        leases: dict = {}
        leases_used = 0
        status: dict = {}
        conflicts: list = []

        def hot_targets(sid):
            return [n for n in targets[sid] if n in hot]

        def start(s, st):
            st["started"], st["remaining"], st["start_ver"], st["attempts"] = True, s.cost, version, 1

        for t in range(horizon):
            for s in sessions:                                   # aging
                st = rt[s.id]
                if s.arrival <= t and not st["done"] and not st["has_lease"]:
                    st["waited"] += 1

            for n in hot:                                        # grant free hot leases by age
                if leases.get(n) is not None:
                    continue
                cands = [s for s in sessions if s.arrival <= t and not rt[s.id]["done"]
                         and not rt[s.id]["has_lease"] and n in targets[s.id]]
                cands.sort(key=lambda s: (-rt[s.id]["waited"], s.arrival, s.id))
                for c in cands:
                    if all(leases.get(d) in (None, c.id) for d in hot_targets(c.id)):
                        for d in hot_targets(c.id):
                            leases[d] = c.id
                        leases_used += 1
                        rt[c.id]["has_lease"] = True
                        if not rt[c.id]["started"]:
                            start(c, rt[c.id])
                        break

            for s in sessions:                                   # disjoint work needs no lease
                st = rt[s.id]
                if s.arrival <= t and not st["done"] and not st["started"] and not hot_targets(s.id):
                    start(s, st)

            ready = []
            for s in sessions:                                   # progress
                st = rt[s.id]
                if not st["done"] and st["started"]:
                    st["remaining"] -= 1
                    if st["remaining"] <= 0:
                        ready.append(s)

            for s in ready:
                st = rt[s.id]
                stale = any(def_version.get(n, 0) > st["start_ver"] for n in targets[s.id])
                if stale:
                    # main moved under us on a target def -> redo (wasted). Leasing prevents this.
                    st["attempts"] += 1
                    st["remaining"], st["start_ver"] = s.cost, version
                    continue

                out = integrate(state, s.variant, s.contracts, self.proposer)
                status[s.id] = (out.tier, out.detail)
                if out.admitted:
                    version += 1
                    for n in out.change.touched:
                        def_version[n] = version
                    st["landed"] = t
                    if self.log is not None and s.context is not None:
                        for n in out.change.touched:
                            src = state.current.get(n)
                            if src is not None:
                                self.log.record(n, src, s.context, agent=s.id)
                else:
                    conflicts.append((s.id, out.conflict_names))
                st["done"] = True
                for d in list(leases):                           # release leases
                    if leases[d] == s.id:
                        leases[d] = None
                st["has_lease"] = False

        return LiveResult(state.current, status, conflicts,
                          {sid: rt[sid]["landed"] for sid in rt},
                          {sid: rt[sid]["attempts"] for sid in rt}, leases_used)
