"""cli.py -- the `braid` command line.

    braid init <file.py>                 start tracking a Python module
    braid status                         show main, contracts, pending sessions
    braid submit <file.py> --id <id> --intent "..." [--contract "assert ..."]...
    braid sessions                       list pending sessions
    braid reconcile [--apply]            fold sessions into main (dry-run unless --apply)
    braid show [<def>]                   print a definition + its content hash
    braid log [<def>]                    provenance history
    braid blame <def>                    who/what produced the current version

Run via the `braid` wrapper or `python3 cli.py ...`.
"""

import argparse
import difflib
import sys

from normalizer import normalize_hash
from repo import BraidError, BraidRepo

TIER = {0: "Tier0 disjoint", 1: "Tier1 dep-coupled", 2: "Tier2 model-merged", 3: "Tier3 ESCALATED"}


def _repo():
    return BraidRepo.find(".")


def cmd_init(args):
    repo = BraidRepo.init(args.file)
    main = repo.load_main()
    print(f"initialized braid repo at {repo.bdir}")
    print(f"tracking {repo.tracked_basename}: {len(main['defs'])} definitions "
          f"({', '.join(main['order'])})")


def cmd_status(args):
    repo = _repo()
    main = repo.load_main()
    print(f"tracking: {repo.tracked_basename}")
    print(f"main: {len(main['defs'])} defs -> {', '.join(main['order'])}")
    print(f"contracts (spec ceiling): {len(main['contracts'])}")
    for cid, src in main["contracts"]:
        print(f"  - {cid}: {src}")
    sessions = repo.load_sessions()
    print(f"pending sessions: {len(sessions)}")
    for s in sessions:
        print(f"  - {s['id']}: \"{s['intent']}\" ({len(s['variant'])} defs, "
              f"{len(s['contracts'])} contracts)")


def cmd_submit(args):
    repo = _repo()
    contracts = []
    for i, c in enumerate(args.contract or []):
        contracts.append((f"{args.id}-c{i}", c))
    repo.submit(args.id, args.file, args.intent or "", contracts, model=args.model)
    print(f"submitted session '{args.id}' from {args.file} "
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
    res, admitted, conflicts = repo.reconcile(apply=args.apply)
    for sid, (tier, detail) in res.status.items():
        mark = "x" if sid in conflicts else "+"
        print(f"  [{mark}] {sid:<16} {TIER[tier]:<20} {detail}")
    n = len(res.status)
    print(f"\n{len(admitted)}/{n} integrated, {len(conflicts)} escalated.")
    if conflicts:
        for sid, names in res.conflicts:
            print(f"  conflict: {sid} on {sorted(names)} (main kept the green version)")
    if args.apply:
        print(f"\napplied -> {repo.tracked_basename} updated; escalated sessions kept pending.")
    else:
        print("\n(dry run -- pass --apply to write main and record provenance)")


def cmd_show(args):
    repo = _repo()
    main = repo.load_main()
    names = [args.name] if args.name else main["order"]
    for name in names:
        if name not in main["defs"]:
            print(f"no definition `{name}`")
            continue
        src = main["defs"][name]
        print(f"# {name}  [{normalize_hash(src)[:12]}]")
        print(src.rstrip() + "\n")


def cmd_log(args):
    repo = _repo()
    names = [args.name] if args.name else repo.load_main()["order"]
    for name in names:
        hist = repo.history(name)
        if not hist:
            continue
        print(f"{name}:")
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


def build_parser():
    p = argparse.ArgumentParser(prog="braid", description="version control for the agentic age")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init"); s.add_argument("file"); s.set_defaults(fn=cmd_init)
    sub.add_parser("status").set_defaults(fn=cmd_status)

    s = sub.add_parser("submit")
    s.add_argument("file")
    s.add_argument("--id", required=True)
    s.add_argument("--intent", default="")
    s.add_argument("--contract", action="append", help="an executable assertion; repeatable")
    s.add_argument("--model", default="unknown")
    s.set_defaults(fn=cmd_submit)

    sub.add_parser("sessions").set_defaults(fn=cmd_sessions)

    s = sub.add_parser("abandon"); s.add_argument("id"); s.set_defaults(fn=cmd_abandon)
    s = sub.add_parser("diff"); s.add_argument("id"); s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("reconcile")
    s.add_argument("--apply", action="store_true", help="write main and record provenance")
    s.set_defaults(fn=cmd_reconcile)

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
