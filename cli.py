"""cli.py -- the `braid` command line.

    braid                                orient: where you are and what to type next
    braid init <file.py|file.go>         start tracking a Python or Go module
    braid status                         show main, contracts, pending sessions
    braid submit <file> --id <id> --intent "..." [--contract "assert ..."]...
    braid sessions                       list pending sessions
    braid reconcile [--apply] [--propose]  fold sessions into main (dry-run unless --apply)
    braid rebuild [--apply] [--offline]  regenerate every def from intent, check against the pins
    braid show [<def>]                   print a definition + its content hash
    braid log [<def>]                    provenance history
    braid blame <def>                    who/what produced the current version
    braid web [--port N]                 browse main, the queue, provenance and rebuild

Run via the `braid` wrapper or `python3 cli.py ...`.
"""

import argparse
import difflib
import os
import sys

import llm
import lang
from repo import BraidError, BraidRepo

TIER = {0: "Tier0 disjoint", 1: "Tier1 dep-coupled", 2: "Tier2 model-merged", 3: "Tier3 ESCALATED"}


def _repo():
    return BraidRepo.find(".")


def cmd_init(args):
    repo = BraidRepo.init(args.path)
    main = repo.load_main()
    ndefs = sum(len(st["order"]) for st in main["files"].values())
    print(f"initialized braid repo at {repo.bdir}")
    print(f"tracking {len(main['files'])} {main['lang']} file(s), {ndefs} definitions:")
    for path, st in sorted(main["files"].items()):
        print(f"  {path}: {', '.join(st['order']) or '(no defs)'}")


def cmd_status(args):
    repo = _repo()
    main = repo.load_main()
    print(f"tracking {len(main['files'])} {main['lang']} file(s):")
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
        print(f"# {unit}  [{lang.normalize_hash(unit, src)[:12]}]")
        print(src.rstrip() + "\n")
        return
    main = repo.load_main()
    for path, st in sorted(main["files"].items()):
        for name in st["order"]:
            src = st["defs"][name]
            print(f"# {path}::{name}  [{lang.normalize_hash(f'{path}::{name}', src)[:12]}]")
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
        print(f"  [~] {unit:<28} {lang.normalize_hash(unit, res.pinned[unit])[:12]} -> "
              f"{lang.normalize_hash(unit, res.rebuilt[unit])[:12]}")
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


def cmd_web(args):
    import web
    web.serve(_repo(), host=args.host, port=args.port)


def cmd_help(args):
    print(overview())


# --- the front door --------------------------------------------------------
#
# Typing the name of a program is a question ("what are you? what now?"), not a syntax
# error. argparse's instinct is to answer it with `error: the following arguments are
# required: cmd`, which is a reprimand for a reasonable act. So a bare `braid` gets an
# orientation instead, written against the state of the directory the user is standing in.

def _summary():
    """(lines, hints) describing the repo here, or None if there isn't one."""
    try:
        repo = BraidRepo.find(".")
        main = repo.load_main()
    except BraidError:
        return None

    units = repo.list_units()
    sessions = repo.load_sessions()
    files = main["files"]
    lines = [
        f"  repo       {repo.root}  ({main['lang']})",
        f"  main       {len(units)} definition(s) across {len(files)} file(s): "
        f"{', '.join(sorted(files))}",
        f"  contracts  {len(main['contracts'])} (the spec ceiling agents cannot weaken)",
    ]
    if sessions:
        who = ", ".join(s["id"] for s in sessions)
        lines.append(f"  pending    {len(sessions)} session(s) waiting to land: {who}")
    else:
        lines.append("  pending    nothing waiting to land")

    hints = [
        ("braid status", "tracked files, the spec ceiling, pending sessions"),
        ("braid show", "a definition and its content hash (`braid show <name>`)"),
        ("braid submit <path> --id <agent> --intent \"...\"", "queue an agent's edit"),
        ("braid reconcile", "what would land; add --apply to write it"),
        ("braid blame <name>", "the agent, intent and model behind a definition"),
        ("braid web", "browse main, the queue and provenance in a browser"),
    ]
    if sessions:
        hints.insert(0, ("braid diff " + sessions[0]["id"], "preview that session against main"))
    return lines, hints


def overview() -> str:
    """What a bare `braid` prints: where you are, and the next useful thing to type."""
    out = ["braid -- version control for the agentic age", ""]
    found = _summary()
    if found is None:
        out += [
            f"  no braid repo here ({os.path.abspath('.')})",
            "",
            "  start tracking code:",
            "    braid init .              every .py or .go file in this directory",
            "    braid init main.go        a single file",
            "",
            "  braid versions definitions by meaning rather than files by bytes, so an agent",
            "  that only reformats your code is a no-op instead of a merge conflict.",
        ]
    else:
        lines, hints = found
        out += lines + ["", "  what you can do:"]
        width = max(len(cmd) for cmd, _ in hints)
        out += [f"    {cmd:<{width}}  {why}" for cmd, why in hints]
    out += ["", "  `braid <command> --help` for a command's options; "
            "`braid help` for this screen."]
    return "\n".join(out)


class _Parser(argparse.ArgumentParser):
    """An argparse parser that suggests instead of only scolding."""

    def __init__(self, *a, example=None, **kw):
        self.example = example
        super().__init__(*a, **kw)

    def error(self, message):
        print(f"{self.prog}: {message}", file=sys.stderr)
        if self.example:
            print(f"\ntry:  {self.example}", file=sys.stderr)
        print(f"\n`{self.prog} --help` lists every option.", file=sys.stderr)
        raise SystemExit(2)


def unknown_command(name: str) -> int:
    """A mistyped command should point at the real one, not just fail."""
    names = sorted(COMMANDS)
    near = difflib.get_close_matches(name, names, n=2, cutoff=0.5)
    print(f"braid: '{name}' is not a braid command.", file=sys.stderr)
    if near:
        print(f"\ndid you mean:  {'  or  '.join('braid ' + n for n in near)}", file=sys.stderr)
    print(f"\nbraid commands: {', '.join(names)}", file=sys.stderr)
    print("run `braid` on its own to get oriented.", file=sys.stderr)
    return 2


def build_parser():
    p = _Parser(prog="braid", description="version control for the agentic age",
                epilog="run `braid` with no arguments to get oriented.")
    sub = p.add_subparsers(dest="cmd", parser_class=_Parser)

    s = sub.add_parser("init", help="track a .py/.go file or a directory of them",
                       example="braid init .")
    s.add_argument("path")
    s.set_defaults(fn=cmd_init)
    sub.add_parser("status", help="tracked files, contracts, pending sessions") \
       .set_defaults(fn=cmd_status)
    sub.add_parser("help", help="what a bare `braid` prints").set_defaults(fn=cmd_help)

    s = sub.add_parser("submit", help="submit an edited file or directory as a session",
                       example='braid submit main.go --id alice --intent "make it idempotent"')
    s.add_argument("path")
    s.add_argument("--id", required=True)
    s.add_argument("--intent", default="")
    s.add_argument("--contract", action="append", help="an executable assertion; repeatable")
    s.add_argument("--as", dest="as_path", help="map a single file to this tracked relpath")
    s.add_argument("--model", default="unknown")
    s.set_defaults(fn=cmd_submit)

    sub.add_parser("sessions", help="list pending sessions").set_defaults(fn=cmd_sessions)

    s = sub.add_parser("abandon", help="drop a pending or escalated session",
                       example="braid abandon alice")
    s.add_argument("id"); s.set_defaults(fn=cmd_abandon)
    s = sub.add_parser("diff", help="preview a pending session against main",
                       example="braid diff alice")
    s.add_argument("id"); s.set_defaults(fn=cmd_diff)

    s = sub.add_parser("reconcile", help="fold pending sessions into main")
    s.add_argument("--apply", action="store_true", help="write main and record provenance")
    s.add_argument("--propose", action="store_true",
                   help="let a model propose Tier-2 merges (still contract-gated)")
    s.set_defaults(fn=cmd_reconcile)

    s = sub.add_parser("rebuild", help="regenerate every definition from intent, check the pins")
    s.add_argument("--apply", action="store_true", help="restore the working tree from the lock")
    s.add_argument("--offline", action="store_true",
                   help="replay the pinned realizations instead of calling a model")
    s.set_defaults(fn=cmd_rebuild)

    s = sub.add_parser("web", help="browse this repo at http://127.0.0.1:7420")
    s.add_argument("--port", type=int, default=7420)
    s.add_argument("--host", default="127.0.0.1")
    s.set_defaults(fn=cmd_web)

    s = sub.add_parser("show", help="print a definition and its content hash")
    s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_show)
    s = sub.add_parser("log", help="provenance history per definition")
    s.add_argument("name", nargs="?"); s.set_defaults(fn=cmd_log)
    s = sub.add_parser("blame", help="who and what produced a definition",
                       example="braid blame greeting")
    s.add_argument("name"); s.set_defaults(fn=cmd_blame)
    return p


COMMANDS = ("init", "status", "help", "submit", "sessions", "abandon", "diff",
            "reconcile", "rebuild", "web", "show", "log", "blame")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if not argv:                                    # `braid` on its own: orient, don't scold
        print(overview())
        return 0
    if argv[0] not in COMMANDS and not argv[0].startswith("-"):
        return unknown_command(argv[0])

    try:
        args = build_parser().parse_args(argv)
    except SystemExit as e:                          # --help (0) or a usage error (2)
        return int(e.code or 0)

    try:
        args.fn(args)
        return 0
    except BraidError as e:
        print(f"braid: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nbraid: interrupted; nothing was written.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
