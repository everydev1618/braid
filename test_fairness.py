"""Zero-dependency TDD spec for livelock/fairness under a moving main (DESIGN.md s.5#1).

Run: python3 test_fairness.py

Claims under test:
  - The optimistic (no-coordination) policy STARVES a slow session on a hot definition.
  - The leased + aging policy guarantees progress: the slow session lands in ONE attempt
    with ZERO wasted re-realization, and lands early rather than last.
  - The fairness machinery engages only on CONTENDED defs; disjoint work needs no leases
    (the Tier-0 common case stays coordination-free) under either policy.
"""

import sys
import traceback

from fairness import Job, simulate_leased, simulate_optimistic


def contended(stream_len):
    """A slow session on `config`, plus a steady stream of fast edits to the same def."""
    jobs = [Job("BIG", frozenset({"config"}), cost=4, arrival=0)]
    for i in range(stream_len):
        jobs.append(Job(f"s{i}", frozenset({"config"}), cost=1, arrival=i))
    return jobs


def disjoint():
    return [Job(f"d{i}", frozenset({f"def{i}"}), cost=2, arrival=0) for i in range(5)]


def test_optimistic_starves_slow_session_on_hot_def():
    r = simulate_optimistic(contended(15), horizon=15)
    big = r.job("BIG")
    assert big.landed_at is None              # never lands -> starved
    assert big.attempts >= 2                  # repeatedly re-realized
    assert r.wasted_work > 0
    smalls = sum(1 for j in r.jobs.values() if j.id.startswith("s") and j.landed_at is not None)
    assert smalls > 0                         # fast sessions sailed past it


def test_leased_rescues_the_slow_session():
    r = simulate_leased(contended(15), horizon=15)
    big = r.job("BIG")
    assert big.landed_at is not None          # it lands...
    assert big.attempts == 1                  # ...in a single attempt (frozen target)
    assert r.wasted_work == 0                 # zero re-realization
    assert r.leases_used > 0                  # leases were the mechanism


def test_leased_finite_scenario_fully_converges():
    r = simulate_leased(contended(6), horizon=30)
    assert r.all_landed                       # everyone lands
    assert r.job("BIG").attempts == 1
    assert r.wasted_work == 0


def test_leased_beats_optimistic_on_the_same_jobs():
    opt = simulate_optimistic(contended(6), horizon=30)
    lea = simulate_leased(contended(6), horizon=30)
    # Both eventually drain, but leased wastes nothing and lands the slow job early, not last.
    assert opt.wasted_work > 0 and lea.wasted_work == 0
    assert opt.job("BIG").attempts > lea.job("BIG").attempts
    assert lea.job("BIG").landed_at < opt.job("BIG").landed_at


def test_disjoint_work_is_coordination_free_under_both_policies():
    for sim in (simulate_optimistic, simulate_leased):
        r = sim(disjoint(), horizon=10)
        assert r.all_landed
        assert r.wasted_work == 0
        assert r.leases_used == 0             # no contention -> no leases, no overhead


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
