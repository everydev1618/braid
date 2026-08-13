# braid

[![CI](https://github.com/everydev1618/braid/actions/workflows/ci.yml/badge.svg)](https://github.com/everydev1618/braid/actions/workflows/ci.yml)

A clean-slate version control system for the agentic age — concurrent agent sessions woven
into one always-green `main`, with no PRs. See [DESIGN.md](DESIGN.md) for the full architecture.

Instead of versioning text and merging lines, braid:

- **normalizes** code to a content-addressed canonical form, so stylistic edits are *no-ops*
  (the conflicts git fights over simply don't exist);
- **auto-merges** changes that are provably independent, and runs **executable contracts** on
  the composed result so it catches `green(A) ∧ green(B) ⇏ green(A∪B)`;
- hands genuine same-definition overlaps to a **model proposer**, admitting the merge only if
  the contract gate passes ("the model proposes, the contract disposes");
- keeps the **generating context** of every change (prompt, files, model) — the thing git
  throws away — deduplicated and keyed by meaning;
- coordinates contended definitions with a **lease + aging** discipline so no session starves;
- can **rebuild** every definition from its recorded intent and check the result against the
  pinned realization hashes — matching hashes mean the intent regenerates *the* program, not
  merely *a* program.

braid tracks **Python or Go**. The engine never mentions a language; a frontend is five
functions registered per file extension in `lang.py`, so `braid init main.go` works the same
way `braid init mymodule.py` does. The two differ in depth, deliberately: Python gets layers 0
and 2 (stdlib `ast` plus a real scope analysis, so local renames are free), Go gets layer 0
(a canonical token stream — comments, formatting and redundant semicolons fold to one hash,
but there is no α-renaming without a parser). Contracts are gated by `exec` for Python and by
`go build` + `go test` for Go, so a Go repo needs `go` on PATH. One repo tracks one language.

Pure Python 3, standard library only. Run tests with `python3 test_*.py` (no pytest needed);
the Go suites skip themselves when `go` is absent. Two exceptions to "no dependencies":
`llm.py`, the seam to a real model, imports the Anthropic SDK lazily, so nothing else —
including the whole test suite — needs it installed; and `contracts_go.py` shells out to the
Go toolchain, which is the only way to know Go code compiles.

Want visuals? `braid web` serves a browser view of a real `.braid/` store on localhost —
main as a lattice of definitions rather than a file tree, the reconcile queue drawn as strands
landing in main, the intent behind any definition, and the rebuild hash table. Read-only,
stdlib-only, no build step.

See the whole thing in one run: `python3 demo_braid.py` (offline and deterministic), or
`python3 demo_braid.py --live` to put a real `claude-opus-5` call behind the Tier-2 merge
proposer and the rebuild realizer.

## Quick start

```sh
# track a single module, or a whole directory of .py (or .go) files
braid init mymodule.py        # or:  braid init .   (run commands from inside the tracked dir)
braid init main.go            # a Go repo: units are func/type/var/const, gated by `go test`

# agents submit edits: a single file (mapped with --as), or a whole edited copy of the tree
braid submit agent_a.py --id alice --as checkout.py \
      --intent "make checkout idempotent" \
      --contract "assert checkout(cart) == checkout(checkout(cart))"
braid submit ./agent_b_worktree --id bob --intent "add discount handling"

# see what would happen, then apply
braid reconcile
braid reconcile --apply        # writes the changed files, records provenance
braid reconcile --apply --propose   # let a model merge same-def overlaps (still contract-gated)

# delete the code and rebuild it from the recorded intent
rm *.py && braid rebuild --apply

# inspect / manage pending work
braid diff alice               # preview a pending session vs main (by meaning)
braid abandon bob              # drop a pending/escalated session
braid status
braid show checkout            # source + content hash (bare name, or path::name)
braid blame checkout           # which agent/intent produced it
braid log                      # provenance history per definition

# or look at all of it in a browser
braid web                      # http://127.0.0.1:7420
```

A session is a set of file edits relative to `main` — a single file (mapped to a tracked path
with `--as`) or a whole edited copy of the tree. braid diffs it by *normalized* meaning, keyed
by `path::name` so the same function name in different files never collides; classifies each
change; gates it on contracts; and (with `--apply`) writes each changed file back and records
who/what produced every definition. Conflicting sessions stay pending for a human; `main`
always stays green. The `.braid/` store lives at the repo root — run commands from inside it,
like `git`.

## Layout

| file | what |
|---|---|
| `normalizer.py` | "semantic gofmt" for Python: canonical content hashing + free-name (dependency) analysis + the module↔units split |
| `normalizer_go.py` | the same four functions for Go, layer 0: a Go lexer, ASI, brace-aware decl splitting |
| `lang.py` | the language registry: which frontend owns a file, a unit, or a codebase |
| `reconciler.py` | commutativity classifier + contract-gated `integrate`/`reconcile` (Tiers 0–3) |
| `contracts.py` | materialize a composed codebase and run executable contracts (Python) |
| `contracts_go.py` | the Go gate: write a scratch module, `go build`, run each contract as a test |
| `merge.py` | the Tier-2 model-proposer seam (`make_llm_proposer` plugs in a real model) |
| `provenance.py` | content-addressed context chunk store + cell log (requirement 1) |
| `fairness.py` | livelock simulation + lease/aging fix (DESIGN §5#1) |
| `live.py` | the unified engine: scheduling + leasing + gating + provenance |
| `llm.py` | the only seam to a real model: Tier-2 merge proposer + rebuild realizer |
| `web.py` | `braid web`: a browser view of a real `.braid/` store, stdlib http.server |
| `repo.py` / `cli.py` / `braid` | on-disk `.braid/` store, CLI, wrapper |
| `demo_braid.py` | the capstone: four beats and an encore |
| `demo_*.py` | runnable demonstrations of each individual capability |

## Rebuild, and what it does and doesn't prove

`main.json` is a lockfile: it holds each unit's pinned realization. `braid rebuild` regenerates
every definition from its recorded intent — never from its own pinned source, which the
`test_realizer_never_sees_the_answer` test enforces — and compares by *normalized* hash. Results
land in three buckets: same meaning as the pin, different-but-contract-green (the residual
decisions an intent underdetermines, DESIGN.md §0), or no recorded intent. `--apply` restores
the working tree from the pin, the way `npm ci` restores from the lock; the regeneration is the
verification, not the source of the restored bytes.

`--offline` replays the pins instead of calling a model. That exercises the mechanism, not the
model — the hash comparison only becomes a real test with credentials present.

## Status

A coherent end-to-end prototype, not a deployable system. All four original goals plus the hard
concurrency problem have working, tested slices (**124 tests, 14 suites**). Not yet built: flake
quarantine, exact incremental test selection, and richer normalization — import sorting,
statement-level commutativity, cross-file dependency tiers, and desugaring (`x += 1` and
`x = x + 1` still hash differently, as do a ternary and its `if`). The Go frontend is layer 0
only: no α-renaming, so Go definitions differing solely in a local variable name hash
differently. See DESIGN.md §3 and §5.
