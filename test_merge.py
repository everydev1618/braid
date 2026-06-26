"""Zero-dependency TDD spec for Tier-2 "model proposes, contract disposes" (DESIGN.md s.4).

Run: python3 test_merge.py

When two sessions change the SAME definition different ways, the reconciler hands the conflict
to a proposer (an LLM in production, a stub here). The invariant under test is that the
PROPOSER IS NEVER TRUSTED: its suggestion is admitted only if it passes the contract gate.
  - a good proposal that satisfies the contracts  -> Tier 2 auto-merged
  - a bad proposal that fails a contract           -> Tier 3 escalated (main keeps ours)
  - no proposer / proposer gives up                -> Tier 3 escalated
"""

import sys
import traceback

from merge import build_merge_prompt, make_llm_proposer
from reconciler import TIER0_DISJOINT, TIER2_MERGED, TIER3_CONFLICT, reconcile


BASE = {"greet": 'def greet(name):\n    return "hi " + name\n'}

# Two agents extend greet incompatibly (same def, different ways):
OURS = {"greet": 'def greet(name):\n    return "hi " + name + "!"\n'}          # adds "!"
THEIRS = {"greet": 'def greet(name):\n    return "hi " + name.capitalize()\n'}  # capitalizes

# Each session brings the contract encoding its own intent.
C_EXCL = ("excl", 'assert greet("bob").endswith("!")')
C_CAP = ("cap", 'assert "Bob" in greet("bob")')

GOOD_MERGE = 'def greet(name):\n    return "hi " + name.capitalize() + "!"\n'   # satisfies both
BAD_MERGE = 'def greet(name):\n    return "hi " + name.capitalize()\n'           # drops "!"


def _sessions():
    return [("A", OURS, [C_EXCL]), ("B", THEIRS, [C_CAP])]


def test_good_proposal_is_auto_merged_tier2():
    res = reconcile(BASE, _sessions(), proposer=lambda req: GOOD_MERGE)
    assert res.status["A"][0] == TIER0_DISJOINT
    assert res.status["B"][0] == TIER2_MERGED
    assert res.merged["greet"] == GOOD_MERGE
    assert not res.conflicts


def test_bad_proposal_is_rejected_by_the_contract_gate():
    # Proposer returns something that violates A's accumulated contract -> not trusted.
    res = reconcile(BASE, _sessions(), proposer=lambda req: BAD_MERGE)
    assert res.status["B"][0] == TIER3_CONFLICT
    assert res.merged["greet"] == OURS["greet"]      # main keeps the already-green version
    assert any(sid == "B" for sid, _ in res.conflicts)


def test_no_proposer_escalates():
    res = reconcile(BASE, _sessions())               # proposer=None
    assert res.status["B"][0] == TIER3_CONFLICT
    assert res.merged["greet"] == OURS["greet"]


def test_proposer_giving_up_escalates():
    res = reconcile(BASE, _sessions(), proposer=lambda req: None)
    assert res.status["B"][0] == TIER3_CONFLICT


def test_proposer_receives_both_sides_and_contracts():
    seen = {}

    def spy(req):
        seen["req"] = req
        return GOOD_MERGE

    reconcile(BASE, _sessions(), proposer=spy)
    req = seen["req"]
    assert req.name == "greet"
    assert req.ours == OURS["greet"] and req.theirs == THEIRS["greet"]
    assert req.base == BASE["greet"]
    assert ("excl", C_EXCL[1]) in req.contracts and ("cap", C_CAP[1]) in req.contracts


def test_llm_proposer_extracts_fenced_code():
    # A realistic model reply with prose + a fenced block; the disposer still gates it.
    def fake_model(prompt):
        assert "OURS" in prompt and "THEIRS" in prompt and "CONTRACTS" in prompt
        return f"Here is the merge:\n```python\n{GOOD_MERGE}```\nDone."

    res = reconcile(BASE, _sessions(), proposer=make_llm_proposer(fake_model))
    assert res.status["B"][0] == TIER2_MERGED
    assert res.merged["greet"].strip() == GOOD_MERGE.strip()


def test_build_merge_prompt_contains_the_pieces():
    from merge import MergeRequest
    req = MergeRequest("greet", BASE["greet"], OURS["greet"], THEIRS["greet"], {}, [C_EXCL])
    p = build_merge_prompt(req)
    assert OURS["greet"] in p and THEIRS["greet"] in p and C_EXCL[1] in p


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
