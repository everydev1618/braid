"""Zero-dependency TDD spec for a braid repo over Go sources (repo.py + lang.py).

Run: python3 test_repo_go.py

Same workflow as test_repo.py -- init, submit, diff, reconcile -- with the Go frontend
underneath, so the claim being tested is that braid's model (units keyed by meaning,
contract-gated reconcile) is language-agnostic and only the frontend is Python-specific.
Tests that need the toolchain to gate a reconcile skip when `go` is not on PATH.
"""

import os
import sys
import tempfile
import traceback

from contracts_go import go_available
from repo import BraidError, BraidRepo, file_state, unit_key

MODULE = '''\
package main

import "fmt"

func greeting() string {
\treturn "Hello, World!"
}

func main() {
\tfmt.Println(greeting())
}
'''

# same meaning, wildly different presentation
RESTYLED = '''\
package main

import "fmt"

// greeting returns the greeting.
func greeting() string {
    return "Hello, World!"
}

func main() { fmt.Println(greeting()) }
'''

CHANGED = '''\
package main

import "fmt"

func greeting() string {
\treturn "Hello, World!"
}

func shout() string {
\treturn "HELLO!"
}

func main() {
\tfmt.Println(greeting())
}
'''


def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _init(d):
    f = _write(os.path.join(d, "main.go"), MODULE)
    return BraidRepo.init(f), f


def test_init_tracks_a_go_file_and_its_decls():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init(d)
        main = repo.load_main()
        assert list(main["files"]) == ["main.go"]
        assert main["files"]["main.go"]["order"] == ["greeting", "main"]
        assert main["lang"] == "go"


def test_init_on_a_directory_discovers_go_files():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "main.go"), MODULE)
        _write(os.path.join(d, "extra.go"), "package main\n\nfunc helper() int { return 1 }\n")
        repo = BraidRepo.init(d)
        assert repo.tracked_files() == ["extra.go", "main.go"]
        assert ("extra.go", "helper") in repo.list_units()


def test_a_repo_tracks_one_language():
    with tempfile.TemporaryDirectory() as d:
        _write(os.path.join(d, "main.go"), MODULE)
        _write(os.path.join(d, "tool.py"), "def f():\n    return 1\n")
        try:
            BraidRepo.init(d)
        except BraidError as e:
            assert "one language" in str(e).lower() or "mixed" in str(e).lower()
        else:
            raise AssertionError("expected a mixed-language init to be refused")


def test_python_repos_are_unaffected():
    with tempfile.TemporaryDirectory() as d:
        f = _write(os.path.join(d, "geo.py"), "def area(r):\n    return 3 * r * r\n")
        repo = BraidRepo.init(f)
        assert repo.load_main()["lang"] == "python"
        assert repo.list_units() == [("geo.py", "area")]


def test_source_and_hash_are_addressable_by_name():
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init(d)
        unit, src = repo.source_of("greeting")
        assert unit == unit_key("main.go", "greeting")
        assert "Hello, World!" in src


def test_a_restyled_submission_is_a_no_op():
    """The headline claim, in Go: reformatting and commenting is not a change."""
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init(d)
        edit = _write(os.path.join(d, "worktree", "main.go"), RESTYLED)
        repo.submit("stylist", edit, "reformat", [], as_path="main.go")
        assert repo.diff("stylist")["items"] == []
        res, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["stylist"] and not conflicts
        assert res.status["stylist"][1] == "no-op (stylistic only)"


def test_a_real_change_reconciles_and_writes_the_file_back():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    with tempfile.TemporaryDirectory() as d:
        repo, path = _init(d)
        edit = _write(os.path.join(d, "worktree", "main.go"), CHANGED)
        repo.submit("alice", edit, "add shout", [("alice-c0", 'if shout() != "HELLO!" { t.Fatal("nope") }')],
                    as_path="main.go")
        items = {i["name"]: i["kind"] for i in repo.diff("alice")["items"]}
        assert items == {"main.go::shout": "added"}
        _, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == ["alice"] and not conflicts
        assert "func shout()" in _read(path)
        assert repo.load_main()["contracts"], "the admitted session's contract joins the ceiling"


def test_a_contract_breaking_session_is_escalated_and_main_stays_green():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    with tempfile.TemporaryDirectory() as d:
        repo, path = _init(d)
        _write(os.path.join(d, "worktree", "main.go"), MODULE.replace("Hello, World!", "Hiya"))
        repo.submit("bob", os.path.join(d, "worktree", "main.go"), "shorten the greeting",
                    [("bob-c0", 'if greeting() != "Hello, World!" { t.Fatal("regressed") }')],
                    as_path="main.go")
        _, admitted, conflicts = repo.reconcile(apply=True)
        assert admitted == [] and conflicts == ["bob"]
        assert "Hello, World!" in _read(path)


def test_the_repo_language_survives_an_apply():
    """`reconcile --apply` rewrites main.json; it must not lose the language."""
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init(d)
        edit = _write(os.path.join(d, "worktree", "main.go"), CHANGED)
        repo.submit("alice", edit, "add shout", [], as_path="main.go")
        repo.reconcile(apply=True)
        assert repo.load_main()["lang"] == "go"
        assert repo._read_json("config.json")["lang"] == "go"


def test_provenance_records_who_produced_a_go_definition():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    with tempfile.TemporaryDirectory() as d:
        repo, _ = _init(d)
        edit = _write(os.path.join(d, "worktree", "main.go"), CHANGED)
        repo.submit("alice", edit, "add shout", [], as_path="main.go")
        repo.reconcile(apply=True)
        cell = repo.blame("shout")
        assert cell.agent == "alice"
        assert repo.context_for_hash(cell.realization_hash).intent == "add shout"


def test_file_state_round_trips_go_through_the_dispatcher():
    st = file_state(MODULE, "main.go")
    assert st["order"] == ["greeting", "main"]
    assert "package main" in st["preamble"]


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
