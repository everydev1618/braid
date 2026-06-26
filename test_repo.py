"""Zero-dependency TDD spec for the on-disk repo / file workflow (repo.py).

Run: python3 test_repo.py
"""

import os
import sys
import tempfile
import traceback

from repo import BraidRepo, parse_module, render_module


def _write(path, text):
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


MODULE = '''\
import math


def area(r):
    return math.pi * r * r


def perimeter(r):
    return 2 * math.pi * r
'''


def _init_repo(d):
    f = _write(os.path.join(d, "geo.py"), MODULE)
    return BraidRepo.init(f), f


def test_parse_and_render_roundtrip_preserves_defs_and_preamble():
    preamble, order, defs = parse_module(MODULE)
    assert "import math" in preamble
    assert order == ["area", "perimeter"]
    out = render_module(preamble, order, defs)
    p2, o2, d2 = parse_module(out)
    assert o2 == order and set(d2) == set(defs)


def test_init_creates_repo_with_main():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        assert os.path.isdir(repo.bdir)
        assert set(repo.load_main()["defs"]) == {"area", "perimeter"}


def test_disjoint_sessions_auto_merge_and_write_file():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_repo(d)
        a = _write(os.path.join(d, "a.py"),
                   MODULE.replace("return 2 * math.pi * r", "return 2.0 * math.pi * r"))
        b = _write(os.path.join(d, "b.py"),
                   MODULE.replace("return math.pi * r * r", "return math.pi * r ** 2"))
        repo.submit("alice", a, "use 2.0", contracts=[])
        repo.submit("bob", b, "use ** for square", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert set(admitted) == {"alice", "bob"} and not conflicts
        on_disk = open(f, encoding="utf-8").read()
        assert "r ** 2" in on_disk and "2.0 * math.pi" in on_disk
        assert repo.load_sessions() == []                 # admitted sessions cleared


def test_same_def_conflict_escalates_main_stays_green():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 1"))
        b = _write(os.path.join(d, "b.py"), MODULE.replace("r * r", "r * r * 2"))
        repo.submit("alice", a, "x1", contracts=[])
        repo.submit("bob", b, "x2", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"] and conflicts == ["bob"]
        assert "r * r * 1" in open(f, encoding="utf-8").read()
        # bob stays pending for a human:
        assert [s["id"] for s in repo.load_sessions()] == ["bob"]


def test_stylistic_only_change_is_a_noop():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        a = _write(os.path.join(d, "a.py"),
                   MODULE.replace("def area(r):\n    return math.pi * r * r",
                                  "def area(radius):\n    return (math.pi * radius * radius)"))
        repo.submit("alice", a, "rename param", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert not conflicts
        assert res.status["alice"][0] == 0                # Tier 0, no-op


def test_failing_contract_escalates():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 0"))  # area always 0
        repo.submit("alice", a, "break area", contracts=[("pos", "assert area(2) > 0")])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert conflicts == ["alice"]
        assert "r * r * 0" not in open(f, encoding="utf-8").read()   # main protected


def test_contract_using_imported_name_passes():
    # Regression: the preamble (imports) must be materialized, or a contract that calls a def
    # using `math` fails with NameError instead of evaluating.
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r ** 2"))
        repo.submit("alice", a, "use **", contracts=[("pi", "assert round(area(1), 5) == 3.14159")])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"] and not conflicts
        assert "r ** 2" in open(f, encoding="utf-8").read()


def test_blame_recovers_provenance_after_reload():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 1"))
        repo.submit("alice", a, "scale area by 1", contracts=[])
        repo.reconcile(apply=True)
        reloaded = BraidRepo.find(d)                       # fresh instance from disk
        cell = reloaded.blame("area")
        assert cell is not None and cell.agent == "alice"
        ctx = reloaded.context_for_hash(cell.realization_hash)
        assert ctx.intent == "scale area by 1"


def test_abandon_removes_pending_session():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 1"))
        repo.submit("alice", a, "x", contracts=[])
        assert [s["id"] for s in repo.load_sessions()] == ["alice"]
        repo.abandon("alice")
        assert repo.load_sessions() == []


def test_diff_reports_modified_and_added():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        variant = MODULE.replace("r * r", "r * r * 2") + "\n\ndef tau():\n    return 2 * math.pi\n"
        a = _write(os.path.join(d, "a.py"), variant)
        repo.submit("alice", a, "scale + add tau", contracts=[])
        diff = repo.diff("alice")
        kinds = {item["name"]: item["kind"] for item in diff["items"]}
        assert kinds == {"area": "modified", "tau": "added"}


def test_diff_noop_for_stylistic_change():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_repo(d)
        a = _write(os.path.join(d, "a.py"), MODULE.replace("def area(r):", "def area(radius):").replace("r * r", "radius * radius"))
        repo.submit("alice", a, "rename", contracts=[])
        assert repo.diff("alice")["items"] == []          # normalizes to current main


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
