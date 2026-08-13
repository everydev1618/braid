"""Zero-dependency TDD spec for the Go contract gate (contracts_go.py).

Run: python3 test_contracts_go.py

A Go contract is executable acceptance criteria, same as the Python one -- except the
executor is the Go toolchain: the composed codebase is written to a scratch module and
each contract runs as a test function. Every test here needs `go` on PATH and is skipped
(loudly) when it is absent, so the suite stays runnable on a Python-only machine.
"""

import sys
import traceback

from contracts_go import go_available, materialize, run_contracts

PREAMBLE = 'package main\n\nimport "fmt"\n'

CODEBASE = {
    "main.go::__preamble__": PREAMBLE,
    "main.go::greeting": 'func greeting() string {\n\treturn "Hello, World!"\n}\n',
    "main.go::main": "func main() {\n\tfmt.Println(greeting())\n}\n",
}

GREEN = ('c-green', 'if greeting() != "Hello, World!" { t.Fatal("wrong greeting") }')
RED = ('c-red', 'if greeting() != "Goodbye" { t.Fatal("wrong greeting") }')


def test_materialize_writes_one_file_per_tracked_path():
    files = materialize(CODEBASE)
    assert set(files) == {"main.go"}
    assert files["main.go"].startswith("package main")
    assert "func greeting()" in files["main.go"] and "func main()" in files["main.go"]


def test_green_contract_passes():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    assert run_contracts(CODEBASE, [GREEN]) == []


def test_failing_contract_is_reported_by_its_id():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    failures = run_contracts(CODEBASE, [GREEN, RED])
    assert [cid for cid, _ in failures] == ["c-red"], failures


def test_the_failure_message_reaches_the_caller():
    """`go test` prints the message *before* the --- FAIL line; don't drop it on the floor."""
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    contract = ("c-loud", 'if greeting() != "nope" { t.Fatalf("greeting regressed: %q", greeting()) }')
    failures = run_contracts(CODEBASE, [contract])
    assert len(failures) == 1, failures
    assert "greeting regressed" in failures[0][1], failures[0][1]
    assert "Hello, World!" in failures[0][1], failures[0][1]


def test_code_that_does_not_compile_is_a_materialize_failure():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    broken = dict(CODEBASE)
    broken["main.go::greeting"] = "func greeting() string {\n\treturn 42\n}\n"
    failures = run_contracts(broken, [GREEN])
    assert failures and failures[0][0] == "<materialize>", failures


def test_no_contracts_still_gates_on_the_build():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    assert run_contracts(CODEBASE, []) == []
    broken = dict(CODEBASE)
    broken["main.go::main"] = "func main() {\n\tundefinedHelper()\n}\n"
    assert run_contracts(broken, []) != []


def test_a_panicking_contract_is_a_failure_not_a_crash():
    if not go_available():
        print("    (skipped: no `go` on PATH)")
        return
    failures = run_contracts(CODEBASE, [("c-panic", 'panic("boom")')])
    assert [cid for cid, _ in failures] == ["c-panic"], failures


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
