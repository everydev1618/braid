"""web.py -- a browser view of a real `.braid/` store. Zero dependencies, stdlib only.

    braid web            # serve http://127.0.0.1:7420 for the repo you're standing in

Four views, because braid has four claims worth seeing:

    /          MAIN      definitions, not files. Every cell carries its content hash.
    /sessions  QUEUE     pending work, classified by tier, drawn as a weave.
    /unit/...  CELL      the intent, prompt, model and context that produced a definition.
    /rebuild   REBUILD   every unit regenerated from intent, checked against the pin.

The whole server is `render(repo, path, query) -> (status, content_type, body)`; the HTTP
handler is a shell around it, so every view is testable without a socket. Read-only: it runs
reconcile as a dry run and rebuild as an offline replay, and never writes to the store.
"""

from __future__ import annotations

import html
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import llm
from normalizer import normalize_hash
from repo import BraidError, BraidRepo, PREAMBLE, SEP, split_unit, unit_key

TIERS = {
    0: ("Tier 0", "disjoint", "t0"),
    1: ("Tier 1", "dep-coupled", "t1"),
    2: ("Tier 2", "model-merged", "t2"),
    3: ("Tier 3", "escalated", "t3"),
}

CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{
  --ink:#14111A; --panel:#1C1825; --panel-2:#221D2E; --rule:#312845;
  --text:#EDE9F5; --muted:#9A92AE; --dim:#6F6885;
  --t0:#5AD1A6; --t1:#7FB2FF; --t2:#C9A2FF; --t3:#FF7A6B; --hash:#F0C674;
  --mono:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",system-ui,sans-serif;
}
html,body{margin:0;padding:0}
body{background:var(--ink);color:var(--text);font-family:var(--sans);font-size:15px;
     line-height:1.55;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
a:focus-visible,button:focus-visible{outline:2px solid var(--t1);outline-offset:2px;border-radius:3px}

/* chrome */
.top{display:flex;align-items:center;gap:20px;padding:14px 28px;border-bottom:1px solid var(--rule);
     background:linear-gradient(180deg,var(--panel) 0%,var(--ink) 100%);position:sticky;top:0;z-index:10;
     flex-wrap:wrap}
.brand{font-family:var(--mono);font-size:19px;letter-spacing:-.06em;font-weight:600}
.brand .b{color:var(--t2)}
.repo{font-family:var(--mono);font-size:13px;color:var(--muted);overflow-wrap:anywhere;
      border-left:1px solid var(--rule);padding-left:20px}
.spacer{flex:1;min-width:0}
.status{font-family:var(--mono);font-size:12px;letter-spacing:.08em;text-transform:uppercase;
        padding:4px 11px;border-radius:100px;border:1px solid}
.status.ok{color:var(--t0);border-color:color-mix(in srgb,var(--t0) 40%,transparent);
           background:color-mix(in srgb,var(--t0) 9%,transparent)}
.status.bad{color:var(--t3);border-color:color-mix(in srgb,var(--t3) 40%,transparent);
            background:color-mix(in srgb,var(--t3) 9%,transparent)}
nav{display:flex;gap:2px;padding:0 22px;border-bottom:1px solid var(--rule);background:var(--ink);
    overflow-x:auto}
nav a{font-family:var(--mono);font-size:13px;padding:11px 14px;color:var(--muted);
      border-bottom:2px solid transparent;white-space:nowrap}
nav a:hover{color:var(--text)}
nav a[aria-current]{color:var(--text);border-bottom-color:var(--t2)}
nav .n{color:var(--dim);margin-left:7px;font-size:11px}
main{max-width:1180px;margin:0 auto;padding:34px 28px 90px}

/* headings */
.eyebrow{font-family:var(--mono);font-size:11px;letter-spacing:.16em;text-transform:uppercase;
         color:var(--dim);margin:0 0 8px}
h1{font-family:var(--mono);font-size:27px;letter-spacing:-.045em;font-weight:600;margin:0 0 8px}
.lede{color:var(--muted);margin:0 0 30px;max-width:64ch;overflow-wrap:anywhere}
h2{font-family:var(--mono);font-size:14px;letter-spacing:-.01em;font-weight:600;
   margin:36px 0 14px;display:flex;align-items:center;gap:10px}
h2::after{content:"";flex:1;height:1px;background:var(--rule)}

/* the cell lattice -- definitions, not a file tree */
.file-label{font-family:var(--mono);font-size:12px;color:var(--dim);margin:26px 0 10px}
.lattice{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(268px,100%),1fr));gap:12px}
.cell{display:block;position:relative;min-width:0;background:var(--panel);border:1px solid var(--rule);
      border-radius:9px;padding:15px 16px 14px 19px;transition:border-color .13s,transform .13s}
.cell::before{content:"";position:absolute;left:0;top:11px;bottom:11px;width:3px;border-radius:3px;
              background:var(--strand,var(--dim))}
.cell:hover{border-color:color-mix(in srgb,var(--t2) 45%,var(--rule));transform:translateY(-1px)}
.cell .nm{font-family:var(--mono);font-size:14.5px;font-weight:600;word-break:break-word}
.cell .hash{font-family:var(--mono);font-size:11.5px;color:var(--hash);margin-top:5px;letter-spacing:.02em}
.cell .by{font-size:12px;color:var(--muted);margin-top:9px;min-width:0;
          overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cell .by .none{color:var(--dim);font-style:italic}

/* the weave -- the signature element */
.weave{background:var(--panel);border:1px solid var(--rule);border-radius:11px;
       padding:6px 0 0;margin:0 0 8px;overflow-x:auto}
.weave svg{display:block;min-width:560px;width:100%}
.legend{display:flex;gap:18px;flex-wrap:wrap;font-family:var(--mono);font-size:11.5px;
        color:var(--muted);padding:0 0 30px}
.legend i{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:6px}

/* rows */
.row{display:flex;gap:16px;align-items:flex-start;background:var(--panel);
     border:1px solid var(--rule);border-radius:9px;padding:15px 17px;margin-bottom:9px}
.row .body{flex:1;min-width:0}
.row .id{font-family:var(--mono);font-size:14px;font-weight:600}
.row .intent{color:var(--muted);margin-top:3px;overflow-wrap:anywhere}
.row .meta{font-family:var(--mono);font-size:11.5px;color:var(--dim);margin-top:8px;
           overflow-wrap:anywhere}
.badge{font-family:var(--mono);font-size:11px;letter-spacing:.06em;padding:4px 9px;border-radius:5px;
       white-space:nowrap;border:1px solid;flex-shrink:0}
.t0{color:var(--t0);border-color:color-mix(in srgb,var(--t0) 38%,transparent);
    background:color-mix(in srgb,var(--t0) 9%,transparent)}
.t1{color:var(--t1);border-color:color-mix(in srgb,var(--t1) 38%,transparent);
    background:color-mix(in srgb,var(--t1) 9%,transparent)}
.t2{color:var(--t2);border-color:color-mix(in srgb,var(--t2) 38%,transparent);
    background:color-mix(in srgb,var(--t2) 9%,transparent)}
.t3{color:var(--t3);border-color:color-mix(in srgb,var(--t3) 38%,transparent);
    background:color-mix(in srgb,var(--t3) 9%,transparent)}

/* detail */
.kv{display:grid;grid-template-columns:132px 1fr;gap:9px 18px;font-size:14px;margin:0 0 26px}
.kv dt{font-family:var(--mono);font-size:12px;color:var(--dim);letter-spacing:.03em}
.kv dd{margin:0;overflow-wrap:anywhere}
.kv dd.hash{font-family:var(--mono);color:var(--hash);font-size:13px}
pre{background:var(--panel);border:1px solid var(--rule);border-radius:9px;padding:16px 18px;
    overflow-x:auto;font-family:var(--mono);font-size:13px;line-height:1.6;margin:0 0 22px}
blockquote{margin:0 0 22px;padding:14px 18px;border-left:3px solid var(--t2);
           background:var(--panel);border-radius:0 9px 9px 0;color:var(--text);overflow-wrap:anywhere}

/* rebuild table */
table{width:100%;border-collapse:collapse;font-size:14px;margin-bottom:26px}
th{text-align:left;font-family:var(--mono);font-size:11px;letter-spacing:.13em;text-transform:uppercase;
   color:var(--dim);font-weight:500;padding:0 12px 9px 0;border-bottom:1px solid var(--rule)}
td{padding:11px 12px 11px 0;border-bottom:1px solid var(--rule);vertical-align:middle}
td.mono{font-family:var(--mono);font-size:12.5px}
td .h{color:var(--hash)}
.mark{font-family:var(--mono);font-size:15px;width:26px}
.mark.same{color:var(--t0)} .mark.diff{color:var(--t2)} .mark.unk{color:var(--dim)}

.note{color:var(--muted);font-size:13.5px;border:1px dashed var(--rule);border-radius:9px;
      padding:13px 16px;margin:0 0 26px}
.empty{color:var(--muted);text-align:center;padding:56px 20px;border:1px dashed var(--rule);
       border-radius:11px}
.empty code{font-family:var(--mono);color:var(--text)}
@media (max-width:640px){
  main{padding:24px 16px 70px} .kv{grid-template-columns:1fr;gap:2px 0}
  .kv dd{margin-bottom:10px} h1{font-size:22px}
  .top{padding:12px 16px;gap:10px}
  .repo{border-left:none;padding-left:0;flex:1 1 auto}
  .status{font-size:10.5px;padding:3px 8px;letter-spacing:.05em}
  nav{padding:0 8px} nav a{padding:11px 10px}
}
@media (prefers-reduced-motion:reduce){*{transition:none!important;animation:none!important}}
"""


# --- html helpers ---------------------------------------------------------

def e(s) -> str:
    return html.escape(str(s), quote=True)


def _agent_color(agent: str) -> str:
    """Stable per-agent strand colour, so one agent's work reads as one thread."""
    palette = ["#5AD1A6", "#7FB2FF", "#C9A2FF", "#F0C674", "#FF9BC4", "#6FE0DC", "#FFAE7A"]
    return palette[sum(ord(c) for c in agent) % len(palette)] if agent else "#6F6885"


def page(title: str, active: str, repo: BraidRepo, body: str, green: bool, counts: dict) -> str:
    nav = []
    for href, label, key in (("/", "main", "main"), ("/sessions", "queue", "sessions"),
                             ("/rebuild", "rebuild", "rebuild")):
        cur = ' aria-current="page"' if key == active else ""
        n = counts.get(key)
        badge = f'<span class="n">{e(n)}</span>' if n is not None else ""
        nav.append(f'<a href="{href}"{cur}>{label}{badge}</a>')
    dot = ('<span class="status ok">main is green</span>' if green
           else '<span class="status bad">main is red</span>')
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title)} &middot; braid</title><style>{CSS}</style></head><body>
<header class="top">
  <span class="brand"><span class="b">~</span>braid</span>
  <span class="repo">{e(os.path.basename(repo.root) or repo.root)}</span>
  <span class="spacer"></span>{dot}
</header>
<nav>{''.join(nav)}</nav>
<main>{body}</main>
</body></html>"""


# --- shared data ----------------------------------------------------------

def _green_and_counts(repo: BraidRepo):
    from contracts import run_contracts
    from repo import units_from_files
    main = repo.load_main()
    contracts = [tuple(c) for c in main["contracts"]]
    failures = run_contracts(units_from_files(main["files"]), contracts)
    counts = {"main": len(repo.list_units()), "sessions": len(repo.load_sessions())}
    return (not failures), counts, main


def _provenance(repo: BraidRepo):
    """unit -> latest cell, for the whole repo, in one pass."""
    log = repo._load_log()
    return {u: log.history_of(u)[-1] for u in
            (unit_key(p, n) for p, n in repo.list_units()) if log.history_of(u)}


# --- view 1: main ---------------------------------------------------------

def view_main(repo: BraidRepo, green: bool, counts: dict, main: dict) -> str:
    prov = _provenance(repo)
    out = [
        '<p class="eyebrow">main</p>',
        "<h1>definitions, not files</h1>",
        '<p class="lede">Every cell below is a unit of <code>main</code>, identified by the '
        "content hash of its canonical form. Two definitions that mean the same thing have the "
        "same hash no matter how they are written &mdash; which is why a restyle is a no-op here "
        "and a merge conflict in git.</p>",
    ]
    for path, st in sorted(main["files"].items()):
        out.append(f'<p class="file-label">{e(path)}</p><div class="lattice">')
        for name in st["order"]:
            src = st["defs"][name]
            unit = unit_key(path, name)
            cell = prov.get(unit)
            agent = cell.agent if cell else ""
            by = (f"{e(agent)} &middot; {e(repo.context_for_hash(cell.realization_hash).intent)}"
                  if cell else '<span class="none">no recorded intent</span>')
            out.append(
                f'<a class="cell" href="/unit/{urllib.parse.quote(unit)}" '
                f'style="--strand:{_agent_color(agent)}">'
                f'<div class="nm">{e(name)}</div>'
                f'<div class="hash">{e(normalize_hash(src)[:16])}</div>'
                f'<div class="by">{by}</div></a>')
        out.append("</div>")
    if main["contracts"]:
        out.append("<h2>the spec ceiling</h2>")
        out.append('<p class="lede">Agents may add contracts. They cannot escape these.</p>')
        out.append("<pre>" + "\n".join(f"{e(cid)}  {e(src)}" for cid, src in main["contracts"])
                   + "</pre>")
    return "".join(out)


# --- view 2: the queue, and the weave ------------------------------------

def weave_svg(strands: list) -> str:
    """The signature element: pending sessions drawn as strands landing in main.

    Every admissible strand reaches the trunk -- that is what landing looks like. What
    separates the tiers is *how*: a Tier 0 strand descends on its own clean diagonal and
    crosses nothing, because it was proven independent of everything else in flight. A Tier 2
    strand contends for a definition, so it crosses another strand and carries a junction mark
    where the contract gate had to rule. A Tier 3 strand stops dead at an X: it never reaches
    main, because no realization satisfied both intents.

    Descent points are staggered left-to-right so the result reads as a weave rather than a
    bundle of parallel lines.
    """
    if not strands:
        return ""
    row, pad, w = 36, 24, 940
    label_w, tail = 112, 96
    n = len(strands)
    h = pad + row * n + 74
    trunk_y = h - pad - 16
    x0 = pad + label_w
    x_end = w - pad - tail
    parts = [f'<svg viewBox="0 0 {w} {h}" role="img" '
             f'aria-label="pending sessions drawn as strands landing in main">']

    # the trunk: always-green main
    parts.append(f'<line x1="{x0 - 26}" y1="{trunk_y}" x2="{w - pad}" y2="{trunk_y}" '
                 f'stroke="#7FE8C0" stroke-width="3.5" stroke-linecap="round"/>')
    parts.append(f'<text x="{w - pad}" y="{trunk_y + 26}" fill="#7FE8C0" font-size="11.5" '
                 f'text-anchor="end" letter-spacing="1.4" '
                 f'font-family="ui-monospace,Menlo,monospace">main</text>')

    span = x_end - x0
    for i, (sid, tier, _names) in enumerate(strands):
        y = pad + 14 + i * row
        colour = {0: "#5AD1A6", 1: "#7FB2FF", 2: "#C9A2FF", 3: "#FF7A6B"}[tier]
        # stagger where each strand turns down, so they interleave instead of stacking
        xd = x0 + span * (0.34 + 0.52 * (i + 1) / (n + 1))
        parts.append(f'<text x="{pad}" y="{y + 4}" fill="#9A92AE" font-size="12" '
                     f'font-family="ui-monospace,Menlo,monospace">{e(sid[:14])}</text>')
        parts.append(f'<circle cx="{x0}" cy="{y}" r="3.5" fill="{colour}"/>')

        if tier == 3:
            stop = x0 + span * 0.52
            parts.append(f'<line x1="{x0}" y1="{y}" x2="{stop}" y2="{y}" stroke="{colour}" '
                         f'stroke-width="2" stroke-linecap="round" opacity=".85"/>')
            for dx1, dy1, dx2, dy2 in ((10, -6, 22, 6), (22, -6, 10, 6)):
                parts.append(f'<line x1="{stop + dx1}" y1="{y + dy1}" x2="{stop + dx2}" '
                             f'y2="{y + dy2}" stroke="{colour}" stroke-width="2.2" '
                             f'stroke-linecap="round"/>')
            continue

        # runs level, then descends into the trunk
        parts.append(f'<path d="M{x0} {y} L{xd} {y} C{xd + 46} {y} {xd + 30} {trunk_y} '
                     f'{xd + 92} {trunk_y}" fill="none" stroke="{colour}" stroke-width="2" '
                     f'stroke-linecap="round"/>')
        parts.append(f'<circle cx="{xd + 92}" cy="{trunk_y}" r="4" fill="{colour}" '
                     f'stroke="#14111A" stroke-width="1.5"/>')
        if tier == 2:
            # contended: mark where the gate had to rule
            parts.append(f'<circle cx="{xd}" cy="{y}" r="6.5" fill="none" stroke="{colour}" '
                         f'stroke-width="1.6"/>')
            parts.append(f'<circle cx="{xd}" cy="{y}" r="2" fill="{colour}"/>')
    parts.append("</svg>")
    return f'<div class="weave">{"".join(parts)}</div>'


def view_sessions(repo: BraidRepo, green: bool, counts: dict, main: dict) -> str:
    sessions = repo.load_sessions()
    head = ['<p class="eyebrow">reconcile queue</p>',
            "<h1>pending work, already classified</h1>",
            '<p class="lede">No branches and no pull requests. Every session is checked against '
            "main and against the others, and each one is admitted, merged, or escalated on the "
            "evidence. This is a dry run &mdash; nothing here writes to the store.</p>"]
    if not sessions:
        return "".join(head) + (
            '<div class="empty">No pending sessions.<br><br>'
            "Submit one with <code>braid submit &lt;file&gt; --id &lt;name&gt; "
            '--intent "&hellip;"</code></div>')

    try:
        res, admitted, conflicts = repo.reconcile(apply=False)
    except BraidError as err:
        return "".join(head) + f'<div class="empty">{e(err)}</div>'

    strands = [(s["id"], res.status[s["id"]][0], []) for s in sessions
               if s["id"] in res.status]
    body = [weave_svg(strands),
            '<div class="legend">'
            '<span><i style="background:#5AD1A6"></i>tier 0 &mdash; independent, lands unattended</span>'
            '<span><i style="background:#7FB2FF"></i>tier 1 &mdash; dep-coupled</span>'
            '<span><i style="background:#C9A2FF"></i>tier 2 &mdash; contended, gate ruled</span>'
            '<span><i style="background:#FF7A6B"></i>tier 3 &mdash; never reaches main</span>'
            "</div>"]

    by_id = {s["id"]: s for s in sessions}
    for sid in sorted(res.status):
        tier, detail = res.status[sid]
        label, meaning, cls = TIERS[tier]
        sd = by_id.get(sid, {})
        files = ", ".join(sorted(sd.get("edits", {}))) or "&mdash;"
        ncon = len(sd.get("contracts", []))
        body.append(
            f'<div class="row"><div class="body">'
            f'<div class="id">{e(sid)}</div>'
            f'<div class="intent">{e(sd.get("intent", "")) or "<em>no intent recorded</em>"}</div>'
            f'<div class="meta">{e(detail)} &middot; touches {files} &middot; '
            f'{ncon} contract{"" if ncon == 1 else "s"}</div>'
            f'</div><span class="badge {cls}">{label} {meaning}</span></div>')

    body.append(f'<p class="note">{len(admitted)} of {len(res.status)} would land with no human '
                f"involved. {len(conflicts)} would reach a person.<br>"
                "This dry run has no model attached, so a same-definition overlap escalates "
                "instead of being offered to a proposer. Run <code>braid reconcile --propose</code> "
                "to let Tier 2 merges through the contract gate.</p>")
    return "".join(head) + "".join(body)


# --- view 3: the cell -----------------------------------------------------

def view_unit(repo: BraidRepo, unit: str, green: bool, counts: dict, main: dict):
    if SEP not in unit:
        return None
    path, name = split_unit(unit)
    st = main["files"].get(path)
    if not st or name not in st.get("defs", {}):
        return None
    src = st["defs"][name]
    h = normalize_hash(src)
    log = repo._load_log()
    hist = log.history_of(unit)
    cell = hist[-1] if hist else None

    out = [f'<p class="eyebrow">{e(path)}</p>', f"<h1>{e(name)}</h1>"]
    if cell:
        ctx = log.context_for(cell.realization_hash)
        out.append('<p class="lede">git would tell you which commit last touched this. '
                   "braid tells you what it was <em>for</em>.</p>")
        out.append(f"<blockquote>{e(ctx.intent)}</blockquote>")
        out.append('<dl class="kv">'
                   f"<dt>agent</dt><dd>{e(cell.agent)}</dd>"
                   f"<dt>model</dt><dd>{e(ctx.model)}</dd>"
                   f"<dt>content hash</dt><dd class='hash'>{e(h)}</dd>"
                   f"<dt>context files</dt><dd>{e(', '.join(ctx.files) or '—')}</dd>"
                   f"<dt>revisions</dt><dd>{len(hist)}</dd>"
                   "</dl>")
        if ctx.prompt and ctx.prompt != ctx.intent:
            out.append("<h2>prompt</h2><pre>" + e(ctx.prompt) + "</pre>")
    else:
        out.append('<p class="lede">This definition predates any recorded session, so there is '
                   "no generating context to show. It came in with <code>braid init</code>.</p>")
        out.append(f'<dl class="kv"><dt>content hash</dt><dd class="hash">{e(h)}</dd></dl>')

    out.append("<h2>realization</h2><pre>" + e(src.rstrip()) + "</pre>")
    if len(hist) > 1:
        out.append("<h2>history</h2><table><tr><th></th><th>agent</th><th>hash</th></tr>")
        for c in reversed(hist):
            out.append(f'<tr><td class="mono">{c.seq}</td><td>{e(c.agent)}</td>'
                       f'<td class="mono"><span class="h">{e(c.realization_hash[:16])}</span></td></tr>')
        out.append("</table>")
    return "".join(out)


# --- view 4: rebuild ------------------------------------------------------

def view_rebuild(repo: BraidRepo, green: bool, counts: dict, main: dict) -> str:
    pins = {unit_key(p, n): main["files"][p]["defs"][n] for p, n in repo.list_units()}
    res = repo.rebuild(llm.replay_realizer(pins))
    out = ['<p class="eyebrow">rebuild</p>',
           "<h1>regenerate, then check the pins</h1>",
           '<p class="lede"><code>main.json</code> is a lockfile: it holds each unit&rsquo;s '
           "pinned realization. Rebuild regenerates every definition from its recorded intent "
           "&mdash; never from its own source &mdash; and compares by normalized hash. Matching "
           "hashes mean the intent rebuilds <em>the</em> program, not merely <em>a</em> program.</p>",
           '<p class="note">This page replays the pinned realizations rather than calling a '
           "model, so it is showing you the mechanism, not a live regeneration. Run "
           "<code>braid rebuild</code> with credentials to make the comparison a real test.</p>",
           "<table><tr><th></th><th>definition</th><th>pinned</th><th>rebuilt</th>"
           "<th>verdict</th></tr>"]
    order = ([(u, "same") for u in res.identical] + [(u, "diff") for u in res.divergent]
             + [(u, "unk") for u in res.missing])
    verdict = {"same": ("identical", "&#9679;"), "diff": ("divergent", "&#9670;"),
               "unk": ("no recorded intent", "&#9675;")}
    for unit, kind in order:
        label, glyph = verdict[kind]
        pin = normalize_hash(res.pinned[unit])[:12]
        new = normalize_hash(res.rebuilt[unit])[:12] if unit in res.rebuilt else "&mdash;"
        out.append(f'<tr><td class="mark {kind}">{glyph}</td>'
                   f'<td class="mono">{e(unit)}</td>'
                   f'<td class="mono"><span class="h">{pin}</span></td>'
                   f'<td class="mono"><span class="h">{new}</span></td>'
                   f"<td>{label}</td></tr>")
    out.append("</table>")
    out.append(f'<p class="note">{len(res.identical)} identical &middot; '
               f"{len(res.divergent)} divergent &middot; {len(res.missing)} without recorded "
               f"intent &middot; contracts "
               f"{'green' if res.green else 'RED'}.</p>")
    return "".join(out)


# --- the router -----------------------------------------------------------

def render(repo: BraidRepo, path: str, query: dict):
    green, counts, main = _green_and_counts(repo)

    def wrap(title, active, body):
        return 200, "text/html; charset=utf-8", page(title, active, repo, body, green, counts)

    if path == "/":
        return wrap("main", "main", view_main(repo, green, counts, main))
    if path == "/sessions":
        return wrap("queue", "sessions", view_sessions(repo, green, counts, main))
    if path == "/rebuild":
        return wrap("rebuild", "rebuild", view_rebuild(repo, green, counts, main))
    if path.startswith("/unit/"):
        unit = urllib.parse.unquote(path[len("/unit/"):])
        body = view_unit(repo, unit, green, counts, main)
        if body is not None:
            return wrap(unit, "main", body)
        return (404, "text/html; charset=utf-8",
                page("not found", "main", repo,
                     f'<div class="empty">No definition <code>{e(unit)}</code> in main.</div>',
                     green, counts))
    return (404, "text/html; charset=utf-8",
            page("not found", "main", repo,
                 '<div class="empty">Nothing here. '
                 '<a href="/" style="color:var(--t1)">Back to main</a>.</div>', green, counts))


# --- the shell ------------------------------------------------------------

def serve(repo: BraidRepo, host: str = "127.0.0.1", port: int = 7420):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = dict(urllib.parse.parse_qsl(parsed.query))
            try:
                status, ctype, body = render(repo, parsed.path, query)
            except Exception as exc:                      # never take the demo down
                status, ctype = 500, "text/plain; charset=utf-8"
                body = f"braid web error: {type(exc).__name__}: {exc}"
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, fmt, *args):
            pass                                          # a quiet terminal during a demo

    httpd = HTTPServer((host, port), Handler)
    print(f"braid web -> http://{host}:{port}   (serving {repo.root}, read-only; ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.server_close()
