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
import textwrap

import llm
import lang
from repo import BraidError, BraidRepo

TIER = {0: "Tier0 disjoint", 1: "Tier1 dep-coupled", 2: "Tier2 model-merged", 3: "Tier3 ESCALATED"}


def _repo():
    return BraidRepo.find(".")


# --- how braid talks -------------------------------------------------------
#
# One voice across every command: count things and inflect them ("1 file", not "1 file(s)"),
# label a block instead of dumping it, never answer with a bare `0` or with silence, and end
# with the next useful thing to type. `(s)` is how a program talks; braid is talking to a
# person who has just arrived.

LABEL = 12          # width of the left-hand label column
WIDTH = 88          # wrap prose here: a report should fit a terminal, not run off it


def plural(n: int, noun: str, suffix: str = "s") -> str:
    return f"{n} {noun}" if n == 1 else f"{n} {noun}{suffix}"


def row(label: str, value: str, wrap: bool = True) -> str:
    """A labelled line, wrapped under its own label rather than off the screen.

    `wrap=False` for values that must survive a copy-paste -- a wrapped path is a broken
    path, and a long one is better off overflowing than mangled.
    """
    indent = " " * (2 + LABEL)
    first = f"  {label:<{LABEL}}{value}"
    if not wrap or len(first) <= WIDTH or " " not in value:
        return first
    lines = textwrap.wrap(value, width=WIDTH - len(indent)) or [value]
    return "\n".join([f"  {label:<{LABEL}}{lines[0]}"] + [indent + ln for ln in lines[1:]])


def sub(value: str, indent: int = LABEL + 2) -> str:
    return " " * indent + value


def clip(text: str, limit: int = 92) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def columns(pairs, indent: int = 4, cap: int = 34) -> list:
    """`command   why` lines, aligned to the longest command but never past the screen."""
    width = min(max(len(cmd) for cmd, _ in pairs), cap)
    pad = " " * indent
    out = []
    for cmd, why in pairs:
        if len(cmd) > width:                       # too long to share a line
            out += [pad + cmd, pad + " " * (width + 2) + why]
        else:
            out.append(f"{pad}{cmd:<{width}}  {why}")
    return out


def nexts(*pairs) -> list:
    """A trailing `next` block: (command, why) pairs, aligned under a `next` label."""
    pairs = [p for p in pairs if p]
    if not pairs:
        return []
    lines = columns(pairs, indent=2 + LABEL)
    first = lines[0].lstrip()
    return ["", f"  {'next':<{LABEL}}{first}"] + lines[1:]


def _provenance_line(repo, main) -> str:
    """How much of main carries a recorded intent -- braid's whole point, so never hidden."""
    units = repo.list_units()
    if not units:
        return "nothing tracked yet"
    log = repo._load_log()
    covered = sum(1 for p, n in units if log.history_of(f"{p}::{n}"))
    if covered == 0:
        return ("no definition carries a recorded intent yet -- nothing has landed through a "
                "session, so `blame` and `rebuild` have nothing to work from")
    if covered == len(units):
        return f"all {plural(covered, 'definition')} carry a recorded intent"
    return (f"{covered} of {len(units)} definitions carry a recorded intent "
            f"(the rest were tracked at init)")


def _session_rows(sessions: list, indent: int = LABEL + 2) -> list:
    width = max(len(s["id"]) for s in sessions)
    out = []
    for s in sessions:
        detail = f"{plural(len(s['edits']), 'file')}"
        if s["contracts"]:
            detail += f", {plural(len(s['contracts']), 'contract')}"
        intent = s["intent"] or "(no intent recorded)"
        out.append(sub(f"{s['id']:<{width}}  \"{clip(intent, 60)}\"  ({detail})", indent))
    return out


def cmd_init(args):
    repo = BraidRepo.init(args.path)
    main = repo.load_main()
    files = main["files"]
    ndefs = sum(len(st["order"]) for st in files.values())
    out = [f"tracking {plural(ndefs, 'definition')} across {plural(len(files), 'file')} "
           f"as {main['lang']}:", ""]
    width = max(len(p) for p in files)
    for path, st in sorted(files.items()):
        out.append(sub(f"{path:<{width}}  {', '.join(st['order']) or '(no definitions)'}", 2))
    out += ["", row("store", repo.bdir, wrap=False)]
    out += nexts(("braid status", "the same picture, any time"),
                 ("braid submit <path> --id <who>", "queue an edit (--intent, --contract)"))
    print("\n".join(out))


def cmd_status(args):
    repo = _repo()
    main = repo.load_main()
    files, sessions = main["files"], repo.load_sessions()
    units = repo.list_units()

    out = [row("repo", f"{repo.root}  ({main['lang']})", wrap=False),
           row("main", f"{plural(len(units), 'definition')} across {plural(len(files), 'file')}")]
    width = max(len(p) for p in files)
    for path, st in sorted(files.items()):
        out.append(sub(f"{path:<{width}}  {', '.join(st['order']) or '(no definitions)'}"))

    if main["contracts"]:
        out.append(row("contracts", f"{plural(len(main['contracts']), 'contract')} in the spec "
                                    "ceiling -- no session may weaken these"))
        cwidth = max(len(cid) for cid, _ in main["contracts"])
        for cid, src in main["contracts"]:
            out.append(sub(f"{cid:<{cwidth}}  {clip(src)}"))
    else:
        out.append(row("contracts", "none yet -- a session's --contract joins the ceiling "
                                    "when it lands"))

    out.append(row("provenance", _provenance_line(repo, main)))

    if sessions:
        out.append(row("pending", f"{plural(len(sessions), 'session')} waiting to land"))
        out += _session_rows(sessions)
        out += nexts((f"braid diff {sessions[0]['id']}", "preview it against main"),
                     ("braid reconcile", "see what would land; --apply writes it"))
    else:
        out.append(row("pending", "nothing waiting to land"))
        out += nexts(("braid submit <path> --id <who>",
                      "queue an edit (--intent, --contract)"))
    print("\n".join(out))


def cmd_submit(args):
    repo = _repo()
    contracts = []
    for i, c in enumerate(args.contract or []):
        contracts.append((f"{args.id}-c{i}", c))
    repo.submit(args.id, args.path, args.intent or "", contracts, model=args.model, as_path=args.as_path)
    gate = (f"gated by {plural(len(contracts), 'contract')}" if contracts
            else "no contracts of its own -- it still has to keep the ceiling green")
    out = [f"queued session '{args.id}' from {args.path}",
           row("intent", args.intent or "(none given -- `braid rebuild` needs one to work from)"),
           row("gate", gate)]
    out += nexts((f"braid diff {args.id}", "see what it changes, by meaning"),
                 ("braid reconcile", "see whether it lands; --apply writes it"))
    print("\n".join(out))


def cmd_sessions(args):
    repo = _repo()
    sessions = repo.load_sessions()
    if not sessions:
        out = ["nothing waiting to land."]
        out += nexts(("braid submit <path> --id <who>",
                      "queue an edit against main"))
        print("\n".join(out))
        return
    out = [f"{plural(len(sessions), 'session')} waiting to land:", ""] + _session_rows(sessions, 2)
    out += nexts((f"braid diff {sessions[0]['id']}", "preview it against main"),
                 ("braid reconcile", "see what would land; --apply writes it"))
    print("\n".join(out))


def cmd_abandon(args):
    repo = _repo()
    repo.abandon(args.id)
    print(f"dropped session '{args.id}'. main is untouched -- nothing of it landed.")


def cmd_diff(args):
    repo = _repo()
    d = repo.diff(args.id)
    print(f"session '{args.id}': \"{d['intent'] or '(no intent recorded)'}\"")
    if not d["items"]:
        print("\n  no effective change: this normalizes to exactly what main already says.")
        print("  reconciling it is a no-op -- which is the point, not a failure.")
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
    swidth = max(len(sid) for sid in res.status) if res.status else 0
    out = []
    for sid, (tier, detail) in res.status.items():
        mark = "x" if sid in conflicts else "+"
        out.append(f"  [{mark}] {sid:<{swidth}}  {TIER[tier]:<20} {clip(detail, 70)}")
    n = len(res.status)
    verb = "landed" if args.apply else "would land"
    stayed = "main stayed green" if args.apply else "main stays green"
    if admitted:
        landed = f"{plural(len(admitted), 'session')} {verb}"
        if n != len(admitted):
            landed += f" of {n}"
    else:
        landed = "nothing landed" if args.apply else "nothing would land"
    out += ["", f"  {landed}" + (f", {plural(len(conflicts), 'session')} escalated to you"
                                 if conflicts else f" -- {stayed}")]
    for sid, names in res.conflicts:
        _, detail = res.status[sid]
        what = "broke a contract" if detail.startswith("contract failure") else "contested"
        out.append(sub(f"{sid} {what} on {', '.join(sorted(names))}; "
                       "main kept the version that was already green", 2))
    if args.apply:
        out += ["", "  written: changed files updated, provenance recorded. escalated sessions "
                    "stay queued."]
        if conflicts:
            out += nexts((f"braid diff {conflicts[0]}", "see what it wanted to change"),
                         (f"braid abandon {conflicts[0]}", "drop it and move on"))
    else:
        out += nexts(("braid reconcile --apply", "write main and record provenance"))
    print("\n".join(out))


def cmd_show(args):
    repo = _repo()
    comment = repo.frontend().line_comment
    if args.name:
        unit, src = repo.source_of(args.name)
        print(f"{comment} {unit}   content hash {lang.normalize_hash(unit, src)[:12]}")
        print(src.rstrip() + "\n")
        return
    main = repo.load_main()
    for path, st in sorted(main["files"].items()):
        for name in st["order"]:
            src = st["defs"][name]
            unit = f"{path}::{name}"
            print(f"{comment} {unit}   content hash {lang.normalize_hash(unit, src)[:12]}")
            print(src.rstrip() + "\n")


def cmd_log(args):
    repo = _repo()
    units = [repo.resolve_unit(args.name)] if args.name else \
            [f"{p}::{n}" for p, n in repo.list_units()]
    shown = 0
    out = []
    for unit in units:
        hist = repo.history(unit)
        if not hist:
            continue
        shown += 1
        out.append(f"{unit}:")
        for cell in hist:
            out.append(sub(f"seq {cell.seq:<3} {cell.agent:<16} "
                           f"[{cell.realization_hash[:12]}]", 2))
    if not shown:
        what = f"'{args.name}' has" if args.name else "no definition here has"
        out = [f"{what} any recorded history yet.",
               "",
               "  braid records who and what produced a definition when a session lands, so a",
               "  tree tracked at `init` starts empty. Submit an edit and reconcile it, and the",
               "  agent, intent, model and context behind it show up here."]
        out += nexts(("braid submit <path> --id <who>", "queue an edit"),
                     ("braid reconcile --apply", "land it and record provenance"))
    print("\n".join(out))


def cmd_blame(args):
    repo = _repo()
    cell = repo.blame(args.name)
    if cell is None:
        out = [f"{args.name} has no recorded provenance.",
               "",
               "  It was tracked when this repo was initialized rather than produced by a",
               "  session, so there is no prompt, model or intent behind it to recover.",
               "  Definitions that land through `braid reconcile` do carry all of that."]
        print("\n".join(out))
        return
    ctx = repo.context_for_hash(cell.realization_hash)
    out = [f"{args.name} was written by {cell.agent}",
           row("intent", ctx.intent or "(none recorded)"),
           row("model", ctx.model),
           row("hash", cell.realization_hash[:16])]
    if ctx.files:
        out.append(row("context", f"{plural(len(ctx.files), 'file')} in scope: "
                                  f"{', '.join(sorted(ctx.files))}"))
    out += nexts((f"braid log {args.name}", "every version of it, in order"))
    print("\n".join(out))


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
    ceiling = (f"{plural(len(main['contracts']), 'contract')} in the spec ceiling"
               if main["contracts"] else "none yet")
    lines = [
        row("repo", f"{repo.root}  ({main['lang']})", wrap=False),
        row("main", f"{plural(len(units), 'definition')} across "
                    f"{plural(len(files), 'file')}: {', '.join(sorted(files))}"),
        row("contracts", ceiling),
    ]
    if sessions:
        who = ", ".join(s["id"] for s in sessions)
        lines.append(row("pending", f"{plural(len(sessions), 'session')} waiting to land: {who}"))
    else:
        lines.append(row("pending", "nothing waiting to land"))

    hints = [
        ("braid status", "this, plus contracts and provenance"),
        ("braid show <name>", "a definition and its content hash"),
        ("braid submit <path> --id <who>", "queue an edit (--intent, --contract)"),
        ("braid reconcile", "what would land; --apply writes it"),
        ("braid blame <name>", "the agent and intent behind a definition"),
        ("braid web", "browse it all in a browser"),
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
        out += lines + ["", "  what you can do:"] + columns(hints)
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
