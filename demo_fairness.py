"""demo_fairness.py -- livelock under a moving main, and the fix (DESIGN.md s.5#1).

Run: python3 demo_fairness.py

A slow session edits a hot definition `config` while a steady stream of fast sessions keep
landing changes to the same def. Under optimistic concurrency the slow session is perpetually
invalidated and starves. A per-contended-def lease with aging freezes the target so the slow
session lands in one shot -- and disjoint work pays none of this cost.
"""

from fairness import Job, simulate_leased, simulate_optimistic


def contended(stream_len):
    jobs = [Job("BIG", frozenset({"config"}), cost=4, arrival=0)]
    for i in range(stream_len):
        jobs.append(Job(f"small-{i}", frozenset({"config"}), cost=1, arrival=i))
    return jobs


def disjoint():
    return [Job(f"feature-{i}", frozenset({f"mod{i}"}), cost=2, arrival=0) for i in range(5)]


def report(r):
    big = r.job("BIG") if "BIG" in r.jobs else None
    line = f"  {r.policy:11} | all_landed={str(r.all_landed):5} | wasted_work={r.wasted_work:2} | leases={r.leases_used:2}"
    if big:
        landed = big.landed_at if big.landed_at is not None else "NEVER"
        line += f" | BIG: {big.attempts} attempt(s), landed={landed}"
    print(line)


def main() -> int:
    H = 15
    print(f"CONTENDED: a cost-4 session on `config` vs a fast edit to `config` every round "
          f"(horizon {H}):")
    report(simulate_optimistic(contended(H), H))
    report(simulate_leased(contended(H), H))
    print("  -> optimistic starves BIG (it never lands); leasing + aging lands it in 1 attempt,\n"
          "     with zero wasted re-realization.\n")

    print("DISJOINT: 5 sessions on 5 different definitions (no contention):")
    report(simulate_optimistic(disjoint(), 10))
    report(simulate_leased(disjoint(), 10))
    print("  -> identical: no leases, no overhead. Fairness machinery engages ONLY on hot defs,\n"
          "     so the Tier-0 common case stays coordination-free.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
