"""fairness.py -- livelock/fairness under a moving main (DESIGN.md s.5#1).

The hard problem: a session that re-realizes against `main` takes time, and `main` keeps
moving while it works. If other sessions keep landing changes to a definition this session
also touches, it is perpetually invalidated -- finish, discover main moved, redo, forever.
That is livelock (and, for a slow session among fast ones, starvation).

This module is a discrete-round SIMULATION (not the real reconciler) used to compare policies:

  optimistic  -- no coordination. A session realizes speculatively against a snapshot; at
                 landing time, if any target def changed since, it is stale and must redo.
                 Demonstrates starvation of a slow session on a hot (contended) definition.

  leased      -- per-contended-definition landing lease + aging. To land changes to a HOT def
                 a session must hold its lease; the lease freezes that def (no one else lands
                 on it), so the holder realizes against a stable target and lands deterministically
                 (one attempt, zero wasted work). Aging grants the lease to the longest-waiting
                 session, guaranteeing progress (no starvation). DISJOINT (non-hot) work needs no
                 lease and lands optimistically -- the Tier-0 common case stays coordination-free.

The simulation is fully deterministic (no wall clock, no randomness).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Job:
    id: str
    targets: frozenset       # definition names this session changes
    cost: int                # rounds to realize one attempt
    arrival: int             # round it enters the system
    # runtime state
    attempts: int = 0
    remaining: int = 0
    base_version: int = -1
    started: bool = False
    has_lease: bool = False
    waited: int = 0
    landed_at: int | None = None


@dataclass
class SimResult:
    policy: str
    all_landed: bool
    wasted_work: int                 # sum over jobs of (attempts-1)*cost
    leases_used: int
    makespan: int | None             # last land round, or None if some never landed
    jobs: dict = field(repr=False)   # id -> final Job

    def job(self, jid: str) -> Job:
        return self.jobs[jid]


def _finish(policy, jobs, leases_used):
    landed = [j for j in jobs if j.landed_at is not None]
    all_landed = len(landed) == len(jobs)
    wasted = sum((j.attempts - 1) * j.cost for j in jobs if j.attempts > 0)
    makespan = max((j.landed_at for j in landed), default=None) if all_landed else None
    return SimResult(policy, all_landed, wasted, leases_used, makespan, {j.id: j for j in jobs})


def simulate_optimistic(jobs: list[Job], horizon: int) -> SimResult:
    main_version = 0
    def_version: dict[str, int] = {}

    for t in range(horizon):
        for j in jobs:                                   # arrivals start their first attempt
            if j.arrival == t:
                j.base_version, j.remaining, j.attempts, j.started = main_version, j.cost, 1, True

        ready = []
        for j in jobs:
            if j.landed_at is None and j.started:
                j.remaining -= 1
                if j.remaining <= 0:
                    ready.append(j)

        for j in ready:                                  # landing serializes within a round
            stale = any(def_version.get(n, 0) > j.base_version for n in j.targets)
            if stale:
                j.attempts += 1                          # re-realize against new main: wasted work
                j.base_version, j.remaining = main_version, j.cost
            else:
                main_version += 1
                for n in j.targets:
                    def_version[n] = main_version
                j.landed_at = t

    return _finish("optimistic", jobs, leases_used=0)


def simulate_leased(jobs: list[Job], horizon: int, hot_threshold: int = 2) -> SimResult:
    main_version = 0
    def_version: dict[str, int] = {}
    leases: dict[str, str | None] = {}
    leases_used = 0

    target_count: dict[str, int] = {}
    for j in jobs:
        for n in j.targets:
            target_count[n] = target_count.get(n, 0) + 1
    hot = {n for n, c in target_count.items() if c >= hot_threshold}

    for t in range(horizon):
        # Age every session that is present, unlanded, and not currently holding a lease.
        for j in jobs:
            if j.arrival <= t and j.landed_at is None and not j.has_lease:
                j.waited += 1

        # Grant each free hot-def lease to the longest-waiting eligible session (aging).
        for n in hot:
            if leases.get(n) is not None:
                continue
            cands = [j for j in jobs if j.arrival <= t and j.landed_at is None
                     and not j.has_lease and n in j.targets]
            cands.sort(key=lambda j: (-j.waited, j.arrival, j.id))
            for cand in cands:
                hot_targets = [d for d in cand.targets if d in hot]
                if all(leases.get(d) in (None, cand.id) for d in hot_targets):
                    for d in hot_targets:
                        leases[d] = cand.id
                    leases_used += 1
                    cand.has_lease = True
                    cand.base_version, cand.remaining, cand.attempts, cand.started = \
                        main_version, cand.cost, 1, True
                    break

        # Disjoint (non-hot) sessions need no lease -- start immediately (Tier-0 fast path).
        for j in jobs:
            if (j.arrival <= t and j.landed_at is None and not j.started
                    and all(d not in hot for d in j.targets)):
                j.base_version, j.remaining, j.attempts, j.started = main_version, j.cost, 1, True

        ready = []
        for j in jobs:
            if j.landed_at is None and j.started:
                j.remaining -= 1
                if j.remaining <= 0:
                    ready.append(j)

        for j in ready:                                  # a lease holder is never stale -> lands
            main_version += 1
            for n in j.targets:
                def_version[n] = main_version
            j.landed_at = t
            for d in list(leases):
                if leases[d] == j.id:
                    leases[d] = None
            j.has_lease = False

    return _finish("leased", jobs, leases_used)
