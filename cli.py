"""cli.py -- the `braid` command line.

    braid init <file.py>                 start tracking a Python module
    braid status                         show main, contracts, pending sessions
    braid submit <file.py> --id <id> --intent "..." [--contract "assert ..."]...
    braid sessions                       list pending sessions
    braid reconcile [--apply] [--propose]  fold sessions into main (dry-run unless --apply)
    braid rebuild [--apply] [--offline]  regenerate every def from intent, check against the pins
    braid show [<def>]                   print a definition + its content hash
    braid log [<def>]                    provenance history
    braid blame <def>                    who/what produced the current version

Run via the `braid` wrapper or `python3 cli.py ...`.
"""

import argparse
import difflib
import sys

import llm
from normalizer import normalize_hash
from repo import BraidError, BraidRepo

TIER = {0: "Tier0 disjoint", 1: "Tier1 dep-coupled", 2: "Tier2 model-merged", 3: "Tier3 ESCALATED"}


def _repo():
    return BraidRepo.find(".")


def cmd_init(args):
    repo = BraidRepo.init(args.path)
    main = repo.load_main()
    ndefs = sum(len(st["order"]) for st in main["files"].values())
    print(f"initialized braid repo at {repo.bdir}")
    print(f"tracking {len(main['files'])} file(s), {ndefs} definitions:")
    for path, st in sorted(main["files"].items()):
        print(f"  {path}: {', '.join(st['order']) or '(no defs)'}")


def cmd_status(args):
    repo = _repo()
    main = repo.load_main()
    print(f"tracking {len(main['files'])} file(s):")
    for path, st in sorted(main["files"].items()):
        print(f"  {path}: {', '.join(st['order']) or '(no defs)'}")
    print(f"contracts (spec ceiling): {len(main['contracts'])}")
    for cid, src in main["contracts"]:
        print(f"  - {cid}: {src}")
    sessions = repo.load_sessions()
    print(f"pending sessions: {len(sessions)}")
    for s in sessions:
        print(f"  - {s['id']}: \"{s['intent']}\" ({len(s['edits'])} file(s), "
              f"{len(s['contracts'])} contract(s))")


def cmd_submit(args):
    repo = _repo()
    contracts = []
    for i, c in enumerate(args.contract or []):
        contracts.append((f"{args.id}-c{i}", c))
    repo.submit(args.id, args.path, args.intent or "", contracts, model=args.model, as_path=args.as_path)
    print(f"submitted session '{args.id}' from {args.path} "
          f"({len(contracts)} contract(s))")


def cmd_sessions(args):
    repo = _repo()
    sessions = repo.load_sessions()
    if not sessions:
        print("no pending sessions")
        return
    for s in sessions:
        print(f"{s['id']:<16} \"{s['intent']}\"")


def cmd_abandon(args):
    repo = _repo()
    repo.abandon(args.id)
    print(f"abandoned pending session '{args.id}'")


def cmd_diff(args):
    repo = _repo()
    d = repo.diff(args.id)
    print(f"session '{args.id}': \"{d['intent']}\"")
    if not d["items"]:
        print("  (no effective change -- normalizes to current main)")
        return
    for item in d["items"]:
        print(f"\n  {item['kind'].upper()} {item['name']}")
        old = (item["old"] or "").splitlines()
        new = (item["new"] or "").splitlines()
        for line in difflib.unified_diff(old, new, lineterm="", n=2,
                                         fromfile="main", tofile=args.id):
            if line.startswith(("---", "+++")):
                continue
            print(f"    {line}")


def cmd_reconcile(args):
    repo = _repo()
    proposer = None
    if args.propose:
        if not llm.available():
            raise BraidError("--propose needs a model; `pip install anthropic` and set "
                             "ANTHROPIC_API_KEY (or run `ant auth login`)")
        proposer = llm.make_merge_proposer()
    res, admitted, conflicts = repo.reconcile(apply=args.apply, proposer=proposer)
    for sid, (tier, detail) in res.status.items():
        mark = "x" if sid in conflicts else "+"
        print(f"  [{mark}] {sid:<16} {TIER[tier]:<20} {detail}")
    n = len(res.status)
    print(f"\n{len(admitted)}/{n} integrated, {len(conflicts)} escalated.")
    if conflicts:
        for sid, names in res.conflicts:
            _, detail = res.status[sid]
            what = "broke contract(s)" if detail.startswith("contract failure") else "contested"
            print(f"  conflict: {sid} {what} {sorted(names)} (main kept the green version)")
    if args.apply:
        print("\napplied -> changed files written; escalated sessions kept pending.")
    else:
        print("\n(dry run -- pass --apply to write main and record provenance)")


def cmd_show(args):
    repo = _repo()
    if args.name:
        unit, src = repo.source_of(args.name)
        print(f"# {unit}  [{normalize_hash(src)[:12]}]")
        print(src.rstrip() + "\n")
        return
    main = repo.load_main()
    for path, st in sorted(main["files"].items()):
        for name in st["order"]:
            src = st["defs"][name]
            print(f"# {path}::{name}  [{normalize_hash(src)[:12]}]")
            print(src.rstrip() + "\n")


def cmd_log(args):
    repo = _repo()
    units = [repo.resolve_unit(args.name)] if args.name else \
            [f"{p}::{n}" for p, n in repo.list_units()]
    for unit in units:
        hist = repo.history(unit)
        if not hist:
            continue
        print(f"{unit}:")
        for cell in hist:
            print(f"  seq {cell.seq:<3} {cell.agent:<16} [{cell.realization_hash[:12]}]")


def cmd_blame(args):
    repo = _repo()
    cell = repo.blame(args.name)
    if cell is None:
        print(f"{args.name}: no provenance (from base, or never reconciled)")
        return
    ctx = repo.context_for_hash(cell.realization_hash)
    print(f"{args.name} <- {cell.agent}")
    print(f"  intent: {ctx.intent}")
    print(f"  model:  {ctx.model}")
    print(f"  hash:   {cell.realization_hash[:16]}")
    if ctx.files:
        print(f"  context files: {', '.join(ctx.files)}")


def cmd_rebuild(args):
    repo = _repo()
    if args.offline:
        main = repo.load_main()["files"]
        realize = llm.replay_realizer({f"{p}::{n}": main[p]["defs"][n] for p, n in repo.list_units()})
    elif not llm.available():
        raise BraidError("no model credentials found; `pip install anthropic` and set "
                         "ANTHROPIC_API_KEY (or run `ant auth login`), or pass --offline")
    else:
        realize = llm.make_llm_realizer()

    try:
        res = repo.rebuild(realize, apply=args.apply)
    except llm.LLMError as e:
        raise BraidError(str(e)) from e

    total = len(res.identical) + len(res.divergent) + len(res.missing)
    print(f"regenerated {total - len(res.missing)}/{total} definitions from recorded intent\n")
    for unit in res.identical:
        print(f"  [=] {unit:<28} same meaning as the pin")
    for unit in res.divergent:
        print(f"  [~] {unit:<28} {normalize_hash(res.pinned[unit])[:12]} -> "
              f"{normalize_hash(res.rebuilt[unit])[:12]}")
    for unit in res.missing:
        print(f"  [?] {unit:<28} no recorded intent (from base, or never reconciled)")

    print(f"\n{len(res.identical)} identical, {len(res.divergent)} divergent, "
          f"{len(res.missing)} unknown")
    if res.failures:
        print(f"contracts: RED ({len(res.failures)} failing)")
        for cid, err in res.failures:
            print(f"  - {cid}: {err}")
    else:
        print("contracts: green")

    if res.exact and res.green:
        print("\nthe intent rebuilds *the* program, not merely *a* program.")
    elif res.divergent and res.green:
        print(f"\n{len(res.divergent)} definition(s) rebuilt differently but stayed green: the "
              "residual decisions the intent underdetermines.")
    if res.missing:
        print(f"{len(res.missing)} definition(s) predate any recorded session, so there is no "
              "intent to rebuild from; they were carried from the pin.")
    if args.apply:
        print("\napplied -> working tree restored from the pinned realization.")


def build_parser():
    p = argparse.ArgumentParser(prog="braid", description="version control for the agentic age")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="track a .py file or a directory of them")
    s.add_argument("path")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("submit", help="submit an edited file or directory as a session")
    s.add_argument("path")
    s.add_argument("--id", required=True)
    s.add_argument("--intent", default="")
    s.add_argument("--contract", action="append", help="an executable assertion; repeatable")
    s.add_argument("--as", dest="as_path", help="map a single file to this tracked relpath")
    s.add_argument("--model", default="unknown")
    s.set_defaults(fn=cmd_submit)

    sub.add_parser("sessions").set_defaults(fn=cmd_sessions)

    s = sub.add_parser("abandon"); s.add_argument("id"); s.set_defaults(fn=cmd_abandon)
    s = sub.add_parser("diff"); s.add_argument("id"); s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("reconcile")
    s.add_argument("--apply", action="store_true", help="write main and record provenance")
    s.add_argument("--propose", action="store_true",
                   help="let a model propose Tier-2 merges (still contract-gated)")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("rebuild", help="regenerate every definition from intent, check the pins")
    s.add_argument("--apply", action="store_true", help="restore the working tree from the lock")
    s.add_argument("--offline", action="store_true",
                   help="replay the pinned realizations instead of calling a model")
    s.set_defaults(fn=cmd_rebuild)

    s = sub.add_parser("show"); s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("log"); s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("blame"); s.add_argument("name"); s.set_defaults(fn=cmd_blame)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        args.fn(args)
        return 0
    except BraidError as e:
        print(f"braid: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
