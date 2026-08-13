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


# --- every command, not just the front door ---------------------------------

ALL_OUTPUTS = ("status", "sessions", "log", "show")


def test_nothing_ever_says_file_s():
    """`2 file(s)` is how a program talks, not a person. Count and inflect."""
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        for cmd in (["status"], ["sessions"], ["log"], []):
            _, out = run(cmd, cwd=d)
            assert "(s)" not in out, f"braid {' '.join(cmd) or '(bare)'}: {out!r}"


def _queue(d, sid="alice"):
    edit = os.path.join(d, f"{sid}.py")
    with open(edit, "w", encoding="utf-8") as f:
        f.write("def area(r):\n    return 3.14159 * r * r\n")
    run(["submit", edit, "--id", sid, "--as", "geo.py", "--intent", "use pi"], cwd=d)


def test_a_dry_run_never_claims_anything_landed():
    """`reconcile` without --apply changes nothing; saying "landed" would be a lie."""
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        _queue(d)
        _, dry = run(["reconcile"], cwd=d)
        assert "would land" in dry, f"a dry run is conditional: {dry!r}"
        assert "landed" not in dry.replace("would land", ""), dry
        _, applied = run(["reconcile", "--apply"], cwd=d)
        assert "landed" in applied, applied


def test_a_repo_path_is_never_wrapped():
    """Wrapping a path breaks copy-paste. Prose wraps; paths do not."""
    with tempfile.TemporaryDirectory() as base:
        d = os.path.join(base, "a-rather-deeply", "nested-working", "directory-with",
                         "a-long-absolute-path")
        os.makedirs(d)
        _repo_dir(d)
        root = os.path.realpath(d)
        _, out = run(["status"], cwd=d)
        assert any(root in line for line in out.splitlines()), \
            f"the repo root should survive on one line: {out!r}"


def test_output_wraps_instead_of_running_off_the_terminal():
    """A friendly report fits an 80-column terminal, or wraps under its own label."""
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        for cmd in (["status"], [], ["log"], ["blame", "area"]):
            _, out = run(cmd, cwd=d)
            for line in out.splitlines():
                if d in line or os.path.realpath(d) in line:
                    continue          # a long tmp path is the path, not prose
                assert len(line) <= 96, f"braid {' '.join(cmd) or '(bare)'}: {len(line)}c: {line!r}"


def test_status_reads_like_a_report():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["status"], cwd=d)
        assert code == 0
        assert d in out or os.path.realpath(d) in out, "say which repo this is"
        assert "1 definition across 1 file" in out, f"inflect singulars: {out!r}"
        assert "geo.py" in out and "area" in out
        assert "next" in out, "a report should end with the next useful thing to type"


def test_status_reports_provenance_coverage():
    """The interesting half of braid is empty on a fresh init; say so rather than hide it."""
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        _, out = run(["status"], cwd=d)
        assert "provenance" in out
        assert "no definition" in out or "0 of 1" in out


def test_status_lists_pending_sessions_with_their_intents():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        edit = os.path.join(d, "edit.py")
        with open(edit, "w", encoding="utf-8") as f:
            f.write("def area(r):\n    return 3.14159 * r * r\n")
        run(["submit", edit, "--id", "alice", "--as", "geo.py", "--intent", "use pi"], cwd=d)
        _, out = run(["status"], cwd=d)
        assert "alice" in out and "use pi" in out
        assert "braid reconcile" in out or "braid diff alice" in out


def test_sessions_empty_state_says_how_to_queue_one():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["sessions"], cwd=d)
        assert code == 0
        assert "braid submit" in out, f"an empty list should teach: {out!r}"


def test_log_with_no_provenance_explains_itself_instead_of_printing_nothing():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["log"], cwd=d)
        assert code == 0
        assert out.strip(), "silence is not an answer"
        assert "reconcile" in out or "session" in out


def test_blame_on_a_base_tracked_definition_explains_why_it_is_empty():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["blame", "area"], cwd=d)
        assert code == 0
        assert "init" in out or "session" in out, f"say why there is nothing: {out!r}"


def test_submit_confirmation_points_at_the_next_step():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        edit = os.path.join(d, "edit.py")
        with open(edit, "w", encoding="utf-8") as f:
            f.write("def area(r):\n    return 3.14159 * r * r\n")
        code, out = run(["submit", edit, "--id", "alice", "--as", "geo.py",
                         "--intent", "use pi"], cwd=d)
        assert code == 0
        assert "alice" in out
        assert "braid diff alice" in out or "braid reconcile" in out


def test_init_points_at_the_next_step():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "geo.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write(MODULE)
        code, out = run(["init", path], cwd=d)
        assert code == 0
        assert "braid status" in out or "braid submit" in out


def test_show_labels_the_hash_in_the_tracked_language_comment_syntax():
    with tempfile.TemporaryDirectory() as d:
        _repo_dir(d)
        code, out = run(["show", "area"], cwd=d)
        assert code == 0
        assert out.lstrip().startswith("#"), "a Python repo's metadata is a Python comment"
        assert "def area(r):" in out, "the source itself stays clean"


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
