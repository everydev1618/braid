"""Zero-dependency TDD spec for the braid web UI (web.py).

`render(repo, path, query) -> (status, content_type, body)` is the whole server: the HTTP
handler is a thin shell around it, so every view is testable without opening a socket.

Run: python3 test_web.py
"""

import os
import sys
import tempfile
import traceback

from repo import BraidRepo
from web import render

STUB = '''\
CART = [{"price": 20.0, "qty": 1}]


def subtotal(cart):
    return 0


def shipping(cart):
    return 0
'''

REAL = '''\
CART = [{"price": 20.0, "qty": 1}]


def subtotal(cart):
    return sum(i["price"] * i["qty"] for i in cart)


def shipping(cart):
    return 0 if subtotal(cart) > 50 else 5
'''


def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _repo(d, pending=True):
    target = _write(os.path.join(d, "checkout.py"), STUB)
    repo = BraidRepo.init(target)
    founder = _write(os.path.join(d, "founder.py"), REAL)
    repo.submit("founder", founder, "implement subtotal and flat shipping",
                [("sub", "assert subtotal(CART) == 20.0")], model="claude-opus-5",
                as_path="checkout.py")
    repo.reconcile(apply=True)
    if pending:
        edit = _write(os.path.join(d, "agent.py"),
                      REAL.replace("else 5", "else 7"))
        repo.submit("ship-agent", edit, "raise flat shipping to $7", [],
                    model="claude-opus-5", as_path="checkout.py")
    return repo


def _get(repo, path, query=None):
    return render(repo, path, query or {})


# --- routing --------------------------------------------------------------

def test_main_view_is_served_at_root():
    with tempfile.TemporaryDirectory() as d:
        status, ctype, body = _get(_repo(d), "/")
        assert status == 200 and "text/html" in ctype
        assert "<!doctype html>" in body.lower()


def test_unknown_route_404s():
    with tempfile.TemporaryDirectory() as d:
        status, _, body = _get(_repo(d), "/nope")
        assert status == 404
        assert "nothing here" in body.lower() or "not found" in body.lower()


# --- view 1: main, units not files ---------------------------------------

def test_main_view_lists_definitions_with_content_hashes():
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(d)
        _, _, body = _get(repo, "/")
        assert "subtotal" in body and "shipping" in body
        from normalizer import normalize_hash
        src = repo.load_main()["files"]["checkout.py"]["defs"]["subtotal"]
        assert normalize_hash(src)[:12] in body, "the content hash is the identity; show it"


def test_main_view_reports_whether_main_is_green():
    with tempfile.TemporaryDirectory() as d:
        _, _, body = _get(_repo(d), "/")
        assert "green" in body.lower()


# --- view 2: the reconcile queue -----------------------------------------

def test_sessions_view_shows_pending_work_and_its_tier():
    with tempfile.TemporaryDirectory() as d:
        _, _, body = _get(_repo(d), "/sessions")
        assert "ship-agent" in body
        assert "raise flat shipping" in body
        assert "Tier" in body


def test_sessions_view_handles_an_empty_queue():
    with tempfile.TemporaryDirectory() as d:
        status, _, body = _get(_repo(d, pending=False), "/sessions")
        assert status == 200, "an empty queue is a normal state, not an error"
        assert "no pending" in body.lower() or "nothing pending" in body.lower()


# --- view 3: blame --------------------------------------------------------

def test_unit_view_shows_the_intent_that_produced_it():
    with tempfile.TemporaryDirectory() as d:
        _, _, body = _get(_repo(d), "/unit/checkout.py::subtotal")
        assert "implement subtotal and flat shipping" in body
        assert "claude-opus-5" in body
        assert "founder" in body


def test_unit_view_404s_on_an_unknown_definition():
    with tempfile.TemporaryDirectory() as d:
        status, _, _ = _get(_repo(d), "/unit/checkout.py::nope")
        assert status == 404


# --- view 4: rebuild ------------------------------------------------------

def test_rebuild_view_buckets_every_unit():
    with tempfile.TemporaryDirectory() as d:
        _, _, body = _get(_repo(d), "/rebuild")
        low = body.lower()
        assert "identical" in low and "divergent" in low
        assert "subtotal" in body


# --- safety ---------------------------------------------------------------

def test_user_supplied_text_is_escaped():
    """Intents are arbitrary text from an agent; they must never render as markup."""
    with tempfile.TemporaryDirectory() as d:
        repo = _repo(d, pending=False)
        edit = _write(os.path.join(d, "x.py"), REAL.replace("else 5", "else 9"))
        repo.submit("xss", edit, "<script>alert(1)</script>", [], as_path="checkout.py")
        _, _, body = _get(repo, "/sessions")
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;" in body


def test_unit_path_traversal_is_rejected():
    with tempfile.TemporaryDirectory() as d:
        status, _, _ = _get(_repo(d), "/unit/../../etc/passwd")
        assert status == 404


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
