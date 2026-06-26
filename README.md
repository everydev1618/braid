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
- coordinates contended definitions with a **lease + aging** discipline so no session starves.

Pure Python 3, standard library only. Run tests with `python3 test_*.py` (no pytest needed).

## Quick start

```sh
# track a single module, or a whole directory of .py files
braid init mymodule.py        # or:  braid init .   (run commands from inside the tracked dir)

# agents submit edits: a single file (mapped with --as), or a whole edited copy of the tree
braid submit agent_a.py --id alice --as checkout.py \
      --intent "make checkout idempotent" \
      --contract "assert checkout(cart) == checkout(checkout(cart))"
braid submit ./agent_b_worktree --id bob --intent "add discount handling"

# see what would happen, then apply
braid reconcile
braid reconcile --apply        # writes the changed files, records provenance

# inspect / manage pending work
braid diff alice               # preview a pending session vs main (by meaning)
braid abandon bob              # drop a pending/escalated session
braid status
braid show checkout            # source + content hash (bare name, or path::name)
braid blame checkout           # which agent/intent produced it
braid log                      # provenance history per definition
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
| `normalizer.py` | "semantic gofmt": canonical content hashing + free-name (dependency) analysis |
| `reconciler.py` | commutativity classifier + contract-gated `integrate`/`reconcile` (Tiers 0–3) |
| `contracts.py` | materialize a composed codebase and run executable contracts |
| `merge.py` | the Tier-2 model-proposer seam (`make_llm_proposer` plugs in a real model) |
| `provenance.py` | content-addressed context chunk store + cell log (requirement 1) |
| `fairness.py` | livelock simulation + lease/aging fix (DESIGN §5#1) |
| `live.py` | the unified engine: scheduling + leasing + gating + provenance |
| `repo.py` / `cli.py` / `braid` | on-disk `.braid/` store, CLI, wrapper |
| `demo_*.py` | runnable demonstrations of each capability |

## Status

A coherent end-to-end prototype, not a deployable system. All four original goals plus the
hard concurrency problem have working, tested slices (54 tests, 8 suites). Not yet built:
flake quarantine, exact incremental test selection, real model wiring, and richer normalization
(import sorting, statement-level commutativity, cross-file dependency tiers). See DESIGN.md §5.
