"""demo_braid.py -- the capstone demo: four beats and an encore.

Run: python3 demo_braid.py            (fully offline, deterministic)
     python3 demo_braid.py --live     (beat 2 uses a real Claude call for the Tier-2 merge)

The beats are ordered so nothing load-bearing can fail live:

  1. THE NO-OP        an agent reformats and renames; braid sees an empty changeset.
  2. THE SWARM        eight agents, one codebase, main green throughout.
  3. THE CONFLICT     the one escalation is a sentence in English, not <<<<<<< HEAD.
  4. BLAME            point at a shipped line, get back the prompt that produced it.
  ENCORE  THE TEARDOWN  rm -rf *.py, rebuild from .braid/, compare the hashes.

Beats 1, 3, 4 and the encore are deterministic and need no network. Beat 2 uses a stub
proposer unless --live is passed and credentials are present.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile

import llm
from normalizer import normalize_hash
from repo import BraidRepo, unit_key

W = 78


def head(n, title):
    print(f"\n{'=' * W}\n  BEAT {n} -- {title}\n{'=' * W}")


def note(text):
    print(f"\n  {text}")


# --- the sample repo ------------------------------------------------------

CHECKOUT = '''\
TAX = 0.08
CART = [{"price": 20.0, "qty": 1}, {"price": 5.0, "qty": 2}]


def subtotal(cart):
    total = 0
    for item in cart:
        total = total + item["price"] * item["qty"]
    return total


def tax(cart):
    return subtotal(cart) * TAX


def shipping(cart):
    return 0 if subtotal(cart) > 50 else 5


def total(cart):
    return subtotal(cart) + tax(cart) + shipping(cart)
'''

STUB = '''\
TAX = 0.08
CART = [{"price": 20.0, "qty": 1}, {"price": 5.0, "qty": 2}]


def subtotal(cart):
    return 0


def tax(cart):
    return 0


def shipping(cart):
    return 0


def total(cart):
    return 0
'''

# CART is defined in the tracked module's preamble, so contracts resolve it at materialize time.
BASE_CONTRACTS = [
    ("subtotal-adds-up", "assert abs(subtotal(CART) - 30.0) < 1e-9"),
    ("total-exceeds-subtotal", "assert total(CART) >= subtotal(CART)"),
]


def demo_proposer(req):
    """A canned stand-in for the model at the Tier-2 seam (see --live for the real thing).

    agent-2 raised the flat rate to $7; agent-7 raised the free-shipping threshold to $100.
    Those are independent edits to one definition -- the union preserves both intents. The
    contract gate, not this function, decides whether it lands.
    """
    if req.name.endswith("::shipping"):
        return "def shipping(cart):\n    return 0 if subtotal(cart) > 100 else 7\n"
    return None

def _write(path, text):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _edit(text, old, new):
    assert old in text, old
    return text.replace(old, new)


def _submit(repo, d, sid, source, intent, contracts=()):
    """Write an agent's edited copy of checkout.py and submit it as a session."""
    path = _write(os.path.join(d, "agents", f"{sid}.py"), source)
    repo.submit(sid, path, intent, list(contracts), model="claude-opus-5", as_path="checkout.py")


# --- beat 1: the no-op ----------------------------------------------------

def beat_no_op(repo, d):
    head(1, "THE NO-OP  (git: a 9-line diff. braid: nothing happened.)")
    restyled = _edit(CHECKOUT, '''\
def subtotal(cart):
    total = 0
    for item in cart:
        total = total + item["price"] * item["qty"]
    return total
''', '''\
def subtotal(basket):
    # accumulate the line items
    running_total = 0

    for line_item in basket:
        running_total = running_total + line_item['price'] * line_item['qty']

    return running_total
''')
    _submit(repo, d, "stylist", restyled, "rename for clarity, add a comment, single quotes")

    before = repo.load_main()["files"]["checkout.py"]["defs"]["subtotal"]
    note("agent 'stylist' renamed the parameter and every local, requoted the strings,")
    print("  and reflowed the body with a comment and blank lines.")
    print("    git would show:   ~9 changed lines, conflicting with anything nearby")
    d_ = repo.diff("stylist")
    print(f"    braid changeset:  {len(d_['items'])} touched definitions")
    print(f"    subtotal hash:    {normalize_hash(before)[:16]}  (unchanged)")
    assert not d_["items"], "the restyle should normalize to a no-op"
    note("The conflict git would have fought over does not exist. Nothing to merge.")
    repo.abandon("stylist")


# --- beat 2: the swarm ----------------------------------------------------

def beat_swarm(repo, d, live):
    head(2, "THE SWARM  (eight agents, one codebase, no branches)")

    # six disjoint agents -- Tier 0, coordination-free
    _submit(repo, d, "agent-1", _edit(CHECKOUT, "TAX = 0.08", "TAX = 0.095"),
            "update sales tax to 9.5%")
    _submit(repo, d, "agent-2", _edit(CHECKOUT, "return 0 if subtotal(cart) > 50 else 5",
                                      "return 0 if subtotal(cart) > 50 else 7"),
            "raise flat shipping to $7")
    _submit(repo, d, "agent-3", CHECKOUT + '''

def item_count(cart):
    return sum(item["qty"] for item in cart)
''', "add an item_count helper",
            [("count", "assert item_count(CART) == 3")])
    _submit(repo, d, "agent-4", CHECKOUT + '''

def is_empty(cart):
    return not cart
''', "add an is_empty predicate", [("empty", "assert is_empty([]) and not is_empty(CART)")])
    _submit(repo, d, "agent-5", CHECKOUT + '''

def average_price(cart):
    return subtotal(cart) / max(1, sum(item["qty"] for item in cart))
''', "add average_price")
    _submit(repo, d, "agent-6", CHECKOUT + '''

def receipt(cart):
    return f"subtotal={subtotal(cart):.2f} total={total(cart):.2f}"
''', "add a receipt formatter")

    # agent-7: touches the SAME definition as agent-2 -> Tier 2, needs a proposer
    _submit(repo, d, "agent-7", _edit(CHECKOUT, "return 0 if subtotal(cart) > 50 else 5",
                                      "return 0 if subtotal(cart) > 100 else 5"),
            "raise the free-shipping threshold to $100")

    # agent-8: genuinely contradicts the spec ceiling -> Tier 3, escalates
    _submit(repo, d, "agent-8", _edit(CHECKOUT,
                                      "return subtotal(cart) + tax(cart) + shipping(cart)",
                                      "return subtotal(cart) - tax(cart)"),
            "make total exclude tax and shipping")

    note(f"8 sessions pending. git would need 8 branches, 8 PRs, and a merge queue.")
    if live and llm.available():
        print("    Tier-2 proposer: real Claude call (claude-opus-5), contract-gated")
        proposer = llm.make_merge_proposer()
    else:
        print("    Tier-2 proposer: canned stub (pass --live for a real call), contract-gated")
        proposer = demo_proposer

    res, admitted, conflicts = repo.reconcile(apply=True, proposer=proposer)
    print()
    from cli import TIER
    for sid, (tier, detail) in sorted(res.status.items()):
        mark = "x" if sid in conflicts else "+"
        print(f"    [{mark}] {sid:<10} {TIER[tier]:<20} {detail}")
    note(f"{len(admitted)}/8 landed unattended. main is green. {len(conflicts)} escalated.")
    return res, conflicts


# --- beat 3: the one real conflict ---------------------------------------

def beat_conflict(repo, res, conflicts):
    head(3, "THE ONE REAL CONFLICT  (a question only a human can answer)")
    if not conflicts:
        note("nothing escalated this run.")
        return
    sessions = {s["id"]: s for s in repo.load_sessions()}
    print("\n    git would hand you this:")
    print("        <<<<<<< HEAD")
    print("            return subtotal(cart) + tax(cart) + shipping(cart)")
    print("        =======")
    print("            return subtotal(cart) - tax(cart)")
    print("        >>>>>>> agent-8")
    print("\n    braid hands you this:")
    for sid, names in res.conflicts:
        tier, detail = res.status[sid]
        intent = sessions.get(sid, {}).get("intent", "?")
        print(f"\n        {sid} wants: \"{intent}\"")
        if detail.startswith("contract failure"):
            broken = ", ".join(sorted(names))
            print(f"        That contradicts the spec ceiling: {broken}")
            print(f"        No realization satisfies both intents. A human has to choose.")
        else:
            print(f"        Contested definitions: {sorted(names)}")
            print(f"        No proposed merge passed the contract gate.")
    note("The only thing that ever interrupts you is a genuine disagreement about")
    print("  what the system should do. Everything else already landed.")


# --- beat 4: blame --------------------------------------------------------

def beat_blame(repo):
    head(4, "BLAME  (git: 'a1b2c3d fix stuff'.  braid: the prompt.)")
    for name in ("item_count", "shipping"):
        cell = repo.blame(name)
        if cell is None:
            continue
        ctx = repo.context_for_hash(cell.realization_hash)
        print(f"\n    $ braid blame {name}")
        print(f"      agent:  {cell.agent}")
        print(f"      intent: \"{ctx.intent}\"")
        print(f"      model:  {ctx.model}")
        print(f"      hash:   {cell.realization_hash[:16]}")
        print(f"      context files: {', '.join(ctx.files)}")
    note("The generating context is versioned alongside the code. git structurally")
    print("  throws this away; braid keeps it, deduplicated and keyed by meaning.")


# --- encore: the teardown -------------------------------------------------

def encore_teardown(repo, live):
    print(f"\n{'=' * W}\n  ENCORE -- THE TEARDOWN\n{'=' * W}")
    tracked = [os.path.join(repo.root, p) for p in repo.tracked_files()]
    for p in tracked:
        os.remove(p)
    note(f"deleted {len(tracked)} tracked file(s). The code is gone; .braid/ remains.")
    print("    $ rm -rf *.py && braid rebuild --apply\n")

    main = repo.load_main()["files"]
    pins = {unit_key(p, n): main[p]["defs"][n] for p, n in repo.list_units()}
    if live and llm.available():
        realize = llm.make_llm_realizer()
        print("    regenerating from intent via claude-opus-5 ...")
    else:
        realize = llm.replay_realizer(pins)
        print("    regenerating from recorded intent (offline replay) ...")

    res = repo.rebuild(realize, apply=True)
    print()
    for unit in res.identical:
        print(f"    [=] {unit:<34} {normalize_hash(res.pinned[unit])[:12]}  same meaning")
    for unit in res.divergent:
        print(f"    [~] {unit:<34} {normalize_hash(res.pinned[unit])[:12]} -> "
              f"{normalize_hash(res.rebuilt[unit])[:12]}")
    for unit in res.missing:
        print(f"    [?] {unit:<34} no recorded intent")

    print(f"\n    {len(res.identical)} identical, {len(res.divergent)} divergent, "
          f"{len(res.missing)} unknown -- contracts: "
          f"{'green' if res.green else 'RED'}")
    assert all(os.path.exists(p) for p in tracked), "the tree should be restored"

    if live and llm.available():
        note("The files are back and the suite is green. Matching hashes mean the intent")
        print("  regenerates *the* program, not merely *a* program. Divergent-but-green")
        print("  definitions are the residual decisions an intent underdetermines.")
    else:
        note("The files are back and the suite is green -- but be honest about what this")
        print("  offline run proves. The replay realizer returns the pinned realizations, so")
        print("  '8 identical' is tautological here: it exercises the *mechanism* (teardown ->")
        print("  regenerate -> compare hashes -> run contracts), not the model. Run with")
        print("  --live to regenerate from intent through claude-opus-5 and make the hash")
        print("  comparison an actual test.")


# --- driver ---------------------------------------------------------------

def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    live = "--live" in argv
    if live and not llm.available():
        print("note: --live requested but no credentials found; running offline.\n")
        live = False

    d = tempfile.mkdtemp(prefix="braid-demo-")
    try:
        target = _write(os.path.join(d, "checkout.py"), STUB)
        repo = BraidRepo.init(target)
        # The founding session: land the real implementation, carrying the human-authored
        # invariants agents may add to but never escape -- and giving every definition a
        # recorded intent, which is what makes the teardown at the end possible.
        _submit(repo, d, "founder", CHECKOUT,
                "implement checkout: subtotal, tax, flat shipping free over $50, and total",
                BASE_CONTRACTS)
        repo.reconcile(apply=True)

        print(f"{'=' * W}\n  braid -- a clean-slate version control system for the agentic age\n"
              f"{'=' * W}")
        print(f"  repo: checkout.py, {len(repo.list_units())} definitions, "
              f"{len(BASE_CONTRACTS)} human-authored contracts (the spec ceiling)")

        beat_no_op(repo, d)
        res, conflicts = beat_swarm(repo, d, live)
        beat_conflict(repo, res, conflicts)
        beat_blame(repo)
        encore_teardown(repo, live)

        print(f"\n{'=' * W}")
        print("  git versions text and merges lines. braid versions meaning and keeps")
        print("  the intent -- so concurrent agents weave into one always-green main.")
        print(f"{'=' * W}\n")
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
