"""Zero-dependency TDD spec for the unified live reconciler (live.py).

Run: python3 test_live.py

This is the capstone: one engine that schedules sessions over time, leases contended defs to
prevent livelock, runs the real contract/merge gate so main stays green, and records provenance.
"""

import sys
import traceback

from normalizer import normalize_hash
from provenance import CellLog, Context
from live import LiveReconciler, LiveSession


def _ctx(prompt):
    return Context(intent=prompt, prompt=prompt, files={}, messages=[],
                   model="claude-opus-4-8", params={})


def union_proposer(req):
    """A competent 'model': merge two list-returning defs by unioning their results."""
    o, t = {}, {}
    exec(req.ours, o)
    exec(req.theirs, t)
    merged = sorted(set(o[req.name]()) | set(t[req.name]()))
    return f"def {req.name}():\n    return {merged!r}\n"


def _flag_session(name, key, cost, arrival):
    return LiveSession(
        id=name,
        variant={"flags": f'def flags():\n    return [{key!r}]\n'},
        contracts=[(f"has_{key}", f'assert {key!r} in flags()')],
        cost=cost, arrival=arrival, context=_ctx(f"add {key} to flags"),
    )


BASE = {"flags": "def flags():\n    return []\n"}


def _contended(stream_len):
    jobs = [_flag_session("BIG", "BIG", cost=4, arrival=0)]
    for i in range(stream_len):
        jobs.append(_flag_session(f"s{i}", f"s{i}", cost=1, arrival=i))
    return jobs


# --- disjoint work: coordination-free through the real gate ---------------

def test_disjoint_sessions_all_land_coordination_free():
    log = CellLog()
    base = {"a": "def a():\n    return 0\n"}
    sessions = [
        LiveSession("A", {**base, "a": "def a():\n    return 1\n"}, cost=1, arrival=0, context=_ctx("a=1")),
        LiveSession("B", {**base, "b": "def b():\n    return 2\n"}, cost=2, arrival=0, context=_ctx("add b")),
        LiveSession("C", {**base, "c": "def c():\n    return 3\n"}, cost=1, arrival=2, context=_ctx("add c")),
    ]
    res = LiveReconciler(base, log=log).run(sessions, horizon=10)
    assert res.all_landed
    assert res.leases_used == 0                # nothing contended
    assert res.wasted_work == 0
    assert {"a", "b", "c"} <= set(res.merged)
    # provenance recovers intent from a line of main:
    assert log.context_for(normalize_hash(res.merged["b"])).intent == "add b"


# --- contended work: leasing prevents starvation, main stays green ---------

def test_leasing_lands_the_slow_session_in_one_attempt():
    res = LiveReconciler(BASE, proposer=union_proposer, use_leases=True).run(_contended(14), horizon=15)
    assert res.landed_round["BIG"] is not None     # not starved
    assert res.attempts["BIG"] == 1                 # frozen target -> single attempt
    assert res.leases_used > 0
    assert "BIG" in _eval_flags(res.merged)         # main actually contains BIG's change


def test_optimistic_starves_the_slow_session():
    res = LiveReconciler(BASE, proposer=union_proposer, use_leases=False).run(_contended(14), horizon=15)
    assert res.landed_round["BIG"] is None          # never lands under contention
    assert res.attempts["BIG"] > 1                   # repeatedly re-realized (wasted)


def test_main_stays_green_under_contention():
    # Every landed flag's contract still holds on the final main (union merges preserved them).
    res = LiveReconciler(BASE, proposer=union_proposer, use_leases=True).run(_contended(6), horizon=40)
    flags = _eval_flags(res.merged)
    for sid, round_ in res.landed_round.items():
        if round_ is not None:
            key = "BIG" if sid == "BIG" else sid
            assert key in flags


# --- genuine contradiction still escalates --------------------------------

def test_contradiction_escalates_and_main_stays_green():
    base = {"flags": "def flags():\n    return []\n"}
    x = LiveSession("X", {"flags": 'def flags():\n    return ["x"]\n'},
                    contracts=[("exact", 'assert flags() == ["x"]')], cost=1, arrival=0, context=_ctx("x only"))
    y = LiveSession("Y", {"flags": 'def flags():\n    return ["y"]\n'},
                    contracts=[("hasy", 'assert "y" in flags()')], cost=1, arrival=0, context=_ctx("add y"))
    res = LiveReconciler(base, proposer=union_proposer, use_leases=True).run([x, y], horizon=10)
    assert res.landed_round["X"] is not None
    assert res.landed_round["Y"] is None                 # Y contradicts X's exact contract
    assert any(sid == "Y" for sid, _ in res.conflicts)
    assert _eval_flags(res.merged) == ["x"]              # main green, holds X


def _eval_flags(codebase):
    ns = {}
    exec(codebase["flags"], ns)
    return ns["flags"]()


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
