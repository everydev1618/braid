"""Zero-dependency TDD spec for the CLI's manners (cli.py).

Run: python3 test_cli.py

Typing `braid` should be answered, not scolded. The claim under test: every way a person
can reasonably get it wrong -- no arguments, a mistyped command, a forgotten flag -- ends in
something that tells them what to do next, and the exit codes still mean what shells expect
(0 for "I helped you", non-zero for "you asked for something I can't do").
"""

import contextlib
import io
import os
import sys
import tempfile
import traceback

import cli
from repo import BraidRepo

MODULE = "def area(r):\n    return 3 * r * r\n"


def run(argv, cwd=None):
    """Invoke the CLI and capture (exit code, stdout+stderr)."""
    out, err = io.StringIO(), io.StringIO()
    prev = os.getcwd()
    if cwd:
        os.chdir(cwd)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
    finally:
        os.chdir(prev)
    return code, out.getvalue() + err.getvalue()


def _repo_dir(d):
    path = os.path.join(d, "geo.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(MODULE)
    BraidRepo.init(path)
    return d


# --- the bare invocation ---------------------------------------------------

def test_bare_invocation_is_not_an_argparse_error():
    with tempfile.TemporaryDirectory() as d:
        code, out = run([], cwd=d)
        assert code == 0, f"exit {code}: typing `braid` is not a usage error"
        assert "the following arguments are required" not in out
        assert "usage: braid [-h]" not in out


def test_bare_invocation_outside_a_repo_offers_init():
    with tempfile.TemporaryDirectory() as d:
        _, out = run([], cwd=d)
        assert "braid init ." in out
        assert "no braid repo" in out.lower()


def test_bare_invocation_inside_a_repo_summarizes_it():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run([], cwd=d)
        assert code == 0
        assert "geo.py" in out or "1 file" in out
        assert "python" in out
        assert "braid reconcile" in out          # the obvious next step


def test_bare_invocation_surfaces_pending_work():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        edit = os.path.join(d, "edit.py")
        with open(edit, "w", encoding="utf-8") as f:
            f.write("def area(r):\n    return 3.14159 * r * r\n")
        run(["submit", edit, "--id", "alice", "--as", "geo.py", "--intent", "use pi"], cwd=d)
        _, out = run([], cwd=d)
        assert "alice" in out or "1 pending" in out


def test_help_is_a_command_too():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["help"], cwd=d)
        assert code == 0
        assert "braid init ." in out


# --- getting it wrong ------------------------------------------------------

def test_a_mistyped_command_suggests_the_real_one():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["stat"], cwd=d)
        assert code != 0
        assert "status" in out, "should suggest the nearest command"
        assert "not a braid command" in out


def test_an_unknown_command_lists_what_there_is():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["frobnicate"], cwd=d)
        assert code != 0
        assert "not a braid command" in out
        assert "reconcile" in out and "submit" in out


def test_a_forgotten_flag_shows_a_working_example():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["submit", "geo.py"], cwd=d)      # no --id
        assert code != 0
        assert "--id" in out
        assert "braid submit" in out, "show a command that would have worked"


def test_a_command_in_a_directory_with_no_repo_says_how_to_make_one():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["status"], cwd=d)
        assert code != 0
        assert "braid init" in out


def test_explicit_help_still_works_and_exits_zero():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["--help"], cwd=d)
        assert code == 0
        assert "reconcile" in out


def test_subcommand_help_still_works():
    with tempfile.TemporaryDirectory() as d:
        code, out = run(["submit", "--help"], cwd=d)
        assert code == 0
        assert "--intent" in out


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
