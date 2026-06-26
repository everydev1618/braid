"""Zero-dependency TDD spec for the on-disk multi-file repo / workflow (repo.py).

Run: python3 test_repo.py
"""

import os
import sys
import tempfile
import traceback

from repo import BraidRepo, parse_module, render_module


def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
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


def _init_file(d):
    f = _write(os.path.join(d, "geo.py"), MODULE)
    return BraidRepo.init(f), f


# --- parsing -------------------------------------------------------------

def test_parse_and_render_roundtrip():
    preamble, order, defs = parse_module(MODULE)
    assert "import math" in preamble and order == ["area", "perimeter"]
    p2, o2, d2 = parse_module(render_module(preamble, order, defs))
    assert o2 == order and set(d2) == set(defs)


# --- single-file (back-compat) -------------------------------------------

def test_init_single_file():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_file(d)
        files = repo.load_main()["files"]
        assert set(files) == {"geo.py"}
        assert set(files["geo.py"]["defs"]) == {"area", "perimeter"}


def test_single_file_disjoint_merge_writes_file():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_file(d)
        _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r ** 2"))
        _write(os.path.join(d, "b.py"), MODULE.replace("2 * math.pi * r", "math.tau * r"))
        repo.submit("alice", os.path.join(d, "a.py"), "use **", contracts=[])
        repo.submit("bob", os.path.join(d, "b.py"), "use tau", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert set(admitted) == {"alice", "bob"} and not conflicts
        out = open(f, encoding="utf-8").read()
        assert "r ** 2" in out and "math.tau * r" in out


def test_same_def_conflict_escalates():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_file(d)
        _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 1"))
        _write(os.path.join(d, "b.py"), MODULE.replace("r * r", "r * r * 2"))
        repo.submit("alice", os.path.join(d, "a.py"), "x1", contracts=[])
        repo.submit("bob", os.path.join(d, "b.py"), "x2", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"] and conflicts == ["bob"]
        assert [s["id"] for s in repo.load_sessions()] == ["bob"]


def test_contract_using_import_passes():
    with tempfile.TemporaryDirectory() as d:
        repo, f = _init_file(d)
        _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r ** 2"))
        repo.submit("alice", os.path.join(d, "a.py"), "**",
                    contracts=[("pi", "assert round(area(1), 5) == 3.14159")])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"] and not conflicts


# --- multi-file ----------------------------------------------------------

PKG_A = "def greet(name):\n    return 'hi ' + name\n"
PKG_B = "def add(a, b):\n    return a + b\n"


def _init_dir(d):
    os.makedirs(os.path.join(d, "pkg"), exist_ok=True)
    _write(os.path.join(d, "pkg", "a.py"), PKG_A)
    _write(os.path.join(d, "pkg", "b.py"), PKG_B)
    return BraidRepo.init(os.path.join(d, "pkg"))


def test_init_directory_tracks_all_files():
    with tempfile.TemporaryDirectory() as d:
        repo = _init_dir(d)
        assert set(repo.tracked_files()) == {"a.py", "b.py"}


def test_two_agents_edit_different_files_auto_merge():
    with tempfile.TemporaryDirectory() as d:
        repo = _init_dir(d)
        root = os.path.join(d, "pkg")
        _write(os.path.join(d, "ea.py"), "def greet(name):\n    return 'hello ' + name\n")
        _write(os.path.join(d, "eb.py"), "def add(a, b):\n    return a + b + 0\n")
        repo.submit("alice", os.path.join(d, "ea.py"), "warmer greet", contracts=[], as_path="a.py")
        repo.submit("bob", os.path.join(d, "eb.py"), "tweak add", contracts=[], as_path="b.py")
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert set(admitted) == {"alice", "bob"} and not conflicts
        assert "hello " in open(os.path.join(root, "a.py"), encoding="utf-8").read()
        assert "a + b + 0" in open(os.path.join(root, "b.py"), encoding="utf-8").read()


def test_same_name_in_different_files_do_not_collide():
    # both files define `helper`; editing one must not touch the other.
    with tempfile.TemporaryDirectory() as d:
        root = os.path.join(d, "pkg")
        os.makedirs(root, exist_ok=True)
        _write(os.path.join(root, "x.py"), "def helper():\n    return 1\n")
        _write(os.path.join(root, "y.py"), "def helper():\n    return 2\n")
        repo = BraidRepo.init(root)
        _write(os.path.join(d, "ex.py"), "def helper():\n    return 10\n")
        repo.submit("alice", os.path.join(d, "ex.py"), "bump x.helper", contracts=[], as_path="x.py")
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"]
        assert "return 10" in open(os.path.join(root, "x.py"), encoding="utf-8").read()
        assert "return 2" in open(os.path.join(root, "y.py"), encoding="utf-8").read()


def test_submitting_whole_dir_adds_new_file():
    with tempfile.TemporaryDirectory() as d:
        repo = _init_dir(d)
        root = os.path.join(d, "pkg")
        # an agent's edited copy of the whole package, with a brand-new module
        copy = os.path.join(d, "copy")
        os.makedirs(copy, exist_ok=True)
        _write(os.path.join(copy, "a.py"), PKG_A)
        _write(os.path.join(copy, "b.py"), PKG_B)
        _write(os.path.join(copy, "c.py"), "def mul(a, b):\n    return a * b\n")
        repo.submit("alice", copy, "add module c", contracts=[])
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"]
        assert os.path.isfile(os.path.join(root, "c.py"))
        assert "c.py" in repo.tracked_files()


def test_blame_resolves_bare_name_after_reload():
    with tempfile.TemporaryDirectory() as d:
        repo = _init_dir(d)
        _write(os.path.join(d, "ea.py"), "def greet(name):\n    return 'yo ' + name\n")
        repo.submit("alice", os.path.join(d, "ea.py"), "yo greeting", contracts=[], as_path="a.py")
        repo.reconcile(apply=True)
        reloaded = BraidRepo.find(os.path.join(d, "pkg"))
        cell = reloaded.blame("greet")              # bare name, unique across files
        assert cell is not None and cell.agent == "alice"
        assert reloaded.context_for_hash(cell.realization_hash).intent == "yo greeting"


def test_abandon_and_diff():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init_file(d)
        _write(os.path.join(d, "a.py"), MODULE.replace("r * r", "r * r * 9"))
        repo.submit("alice", os.path.join(d, "a.py"), "scale", contracts=[])
        diff = repo.diff("alice")
        assert [it["kind"] for it in diff["items"]] == ["modified"]
        assert "geo.py::area" in diff["items"][0]["name"]
        repo.abandon("alice")
        assert repo.load_sessions() == []


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
