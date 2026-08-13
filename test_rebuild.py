"""Zero-dependency TDD spec for `braid rebuild` (repo.rebuild).

The claim under test: because braid keeps the generating intent alongside the code, a tracked
repo can be regenerated from `.braid/` and the result *checked against the pinned realization
hashes*. Matching hashes prove we regenerated **the** program, not merely *a* program.

`main.json` is the lockfile: it holds the pinned realization of each unit. Regeneration must
therefore never read a unit's own source -- only its intent + context. `test_realizer_never_
sees_the_answer` is the test that keeps that honest.

Run: python3 test_rebuild.py
"""

import os
import sys
import tempfile
import traceback

from normalizer import normalize_hash
from repo import BraidRepo, unit_key

BASE = '''\
import math


def area(r):
    return 0


def perimeter(r):
    return 0
'''

REAL = '''\
import math


def area(r):
    return math.pi * r * r


def perimeter(r):
    return 2 * math.pi * r
'''

CONTRACTS = [("area-positive", "assert area(1) > 3.14 and area(1) < 3.15"),
             ("perim-positive", "assert perimeter(1) > 6.28 and perimeter(1) < 6.29")]


def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _repo_with_provenance(d):
    """init a stub module, then land a real session so every def has a recorded cell."""
    target = _write(os.path.join(d, "geo.py"), BASE)
    repo = BraidRepo.init(target)
    edit = _write(os.path.join(d, "agent.py"), REAL)
    repo.submit("alice", edit, "implement circle geometry with math.pi",
                CONTRACTS, model="test-model", as_path="geo.py")
    repo.reconcile(apply=True)
    return repo, target


def _replay(sources):
    """A realizer that returns canned source per unit key."""
    def realize(unit, context, contracts):
        return sources[unit]
    return realize


# --- the happy path ------------------------------------------------------

def test_rebuild_reproduces_pinned_hashes():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        res = repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in main.items()}))
        assert set(res.identical) == {"geo.py::area", "geo.py::perimeter"}, res.identical
        assert res.divergent == [] and res.missing == []
        assert res.exact and res.green, res.failures


def test_stylistic_variant_still_counts_as_identical():
    """The rebuild is compared by *meaning*, not bytes: a renamed param is the same program."""
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        variant = dict(main)
        variant["area"] = "def area(radius):\n    return math.pi * radius * radius\n"
        res = repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in variant.items()}))
        assert res.exact, (res.divergent, res.missing)
        assert variant["area"] != main["area"]          # different bytes...
        assert normalize_hash(variant["area"]) == normalize_hash(main["area"])   # ...same meaning


# --- the honest failure modes -------------------------------------------

def test_divergent_realization_is_reported_not_hidden():
    """A different-but-green realization is the residual-decisions argument made visible."""
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        variant = dict(main)
        variant["area"] = "def area(r):\n    return math.pi * (r ** 2)\n"
        res = repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in variant.items()}))
        assert res.divergent == ["geo.py::area"], res.divergent
        assert res.identical == ["geo.py::perimeter"]
        assert not res.exact
        assert res.green, res.failures        # different realization, contracts still pass


def test_red_rebuild_is_caught_by_the_contract_gate():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        broken = dict(main)
        broken["area"] = "def area(r):\n    return -1\n"
        res = repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in broken.items()}))
        assert not res.green
        assert [cid for cid, _ in res.failures] == ["area-positive"], res.failures


def test_units_without_provenance_are_reported_missing():
    with tempfile.TemporaryDirectory() as d:
        target = _write(os.path.join(d, "geo.py"), REAL)
        repo = BraidRepo.init(target)          # init records no cells
        res = repo.rebuild(_replay({}))
        assert set(res.missing) == {"geo.py::area", "geo.py::perimeter"}
        assert res.identical == [] and not res.exact


# --- the honesty guarantee ----------------------------------------------

def test_realizer_never_sees_the_answer():
    """Regeneration must come from intent, not from the pinned source hiding in the context."""
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        seen = {}

        def realize(unit, context, contracts):
            seen[unit] = context
            return main[unit.split("::", 1)[1]]

        repo.rebuild(realize)
        area_ctx = seen["geo.py::area"]
        blob = "\n".join(area_ctx.files.values())
        assert "math.pi * r * r" not in blob, "the target's realization leaked into its context"
        assert "def area" not in blob, "the target definition leaked into its context"
        assert "def perimeter" in blob, "sibling definitions are legitimate context"
        assert area_ctx.intent == "implement circle geometry with math.pi"


def test_realizer_receives_the_contracts_to_aim_at():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        seen = {}

        def realize(unit, context, contracts):
            seen[unit] = [cid for cid, _ in contracts]
            return main[unit.split("::", 1)[1]]

        repo.rebuild(realize)
        assert "area-positive" in seen["geo.py::area"]


# --- the teardown ---------------------------------------------------------

def test_apply_restores_deleted_files_from_braid():
    with tempfile.TemporaryDirectory() as d:
        repo, target = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        os.remove(target)
        assert not os.path.exists(target)
        res = repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in main.items()}),
                           apply=True)
        assert res.exact and res.green
        assert os.path.exists(target)
        restored = open(target, encoding="utf-8").read()
        assert "import math" in restored, "the preamble is carried from the lockfile"
        assert "math.pi * r * r" in restored


def test_rebuild_without_apply_does_not_touch_the_tree():
    with tempfile.TemporaryDirectory() as d:
        repo, target = _repo_with_provenance(d)
        main = repo.load_main()["files"]["geo.py"]["defs"]
        os.remove(target)
        repo.rebuild(_replay({unit_key("geo.py", n): s for n, s in main.items()}))
        assert not os.path.exists(target)


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
