# braid — a version control system for the agentic age

> Concurrent agent sessions, woven into one green main. (Earlier working name: antigit.)

## 0. Thesis

Git was built around **scarce, sequential, human commits**. A commit is a content-addressed
*snapshot* of bytes; history is humans communicating diffs to humans; concurrency is the
exception, gated on human attention (branch → PR → review → merge).

Agents invert every one of those assumptions: commits are cheap and machine-paced,
concurrency is the default operating mode, the meaningful unit is an *intent* not a diff,
and human review cannot sit on the critical path at agent throughput.

Three requirements drove this design:

1. **Keep the context.** Preserve what was in the developer/agent context when code was
   generated — the prompt, retrieved files, conversation, model, tools, params. Git
   structurally throws this away.
2. **It's not (primarily) about the code.** The durable artifact is the *intent + context +
   contract*; the code is a build output.
3. **Concurrency without conflicts.** Many agents work the same codebase simultaneously and
   integrate without manual merge resolution.

### The core insight

These three requirements are **not co-satisfiable by a single source of truth.** Req 1 is
free (pure addition). Reqs 2 and 3 pull toward *intent-as-truth*. But intent-as-truth alone
forfeits reproducibility, debuggability, and the **residual decisions** that exist only in
the bytes (the 10,000 micro-choices an intent underdetermines: error handling, edge-case
guards, the actual algorithm).

The resolution: **don't pick one source of truth. Version code and intent+context as a
linked, bidirectional pair — neither derived-and-discarded.** The radical move is not
demoting code; it's *promoting intent to code's equal* and linking them so tightly you can
move between them and keep them from drifting.

> **`intent` is the source file. `context` is the compiler's input environment. `contract`
> is the type signature. `realization` is the compiled binary — pinned in a lockfile,
> regenerated only on demand. Concurrency is composing intents whose contracts don't
> contradict; "merge" is re-realizing the composed set and proving it green.**

## 1. The four-layer stack

```
1. SPEC / intent / contract      ← human-authored source of truth for WHAT
   (spec-driven development)        mergeable SEMANTICALLY; the agent-can't-weaken ceiling
        │  provenance link (req 1) — context captured here
        ▼
2. REALIZATION = content-addressed typed AST   ← canonical identity of the code
   (Unison-style)                                kills syntactic conflicts (req 3); exact hashing/caching
        │
        ├──► 3. TEXT SOURCE  = a rendered VIEW of the AST   ← read + hand-edit; regenerated, not stored
        │
        └──► 4. BYTECODE/WASM = execution & distribution    ← run contracts here; ship this
```

- **Req 1 (context)** attaches at the 1↔2 link.
- **Req 2 ("not about the code")** becomes precise: the *text* (layer 3) genuinely isn't the
  artifact — it's a view. But the *AST* (layer 2) is still ground truth for execution. We can
  honestly say "it's not about the code" about the text while keeping a reproducible canonical
  artifact.
- **Req 3 (concurrency)** is served twice: agents append at layer 1 (semantic, no textual
  conflict), and even the realization at layer 2 has no *syntactic* conflicts to begin with.

### One-line summary

Specs are the source, the AST is the binary, text is a view, bytecode is the shipping
container — and the VCS keeps all four in lockstep, which is the thing nothing today does.

### On inventing a language

Do **not** invent a new *surface* syntax — LLMs are good at a language in proportion to its
pretraining corpus; a greenfield language is "AI-era" precisely in that AI is bad at it.
Instead:

- **Own the canonical IR** (layer 2) — internal, never written by hand or emitted by the model.
- **Borrow the surface** from a high-corpus language (TS/Python/Go). Optionally constrain it
  to a verifiable, contract-annotated *subset* — keep the corpus, add the properties.
- **Canonicalize after generation.** The model emits familiar, redundant code; a deterministic
  normalizer ("semantic gofmt") folds it into the canonical form. The reliability win and the
  corpus win stop being in tension.
- Inventing a language *and* a VCS is two moonshots (cf. Unison, Darklang). Ship the VCS on
  existing languages first; lower the surface later if the thesis demands it.

## 2. The Cell — the atomic object

History is **not** a chain of snapshots (git) or a flat set of patches (Pijul). The atom is a
context-bearing intent:

```
Cell {
  intent:       "make checkout idempotent"          // the what
  context:      <prompt, retrieved files, convo,     // req 1: the why + how
                 model id, tool defs, params>         //   content-addressed, dedup'd
  contract:     <tests | types | properties>         // what "done" means, executable
  realization:  <canonical AST>                      // req 2: a cached OUTPUT, content-addressed, pinned
  provenance:   <agent, parent cells, timestamp>
  deps:         [other cells this assumes]
}
```

- **History = a DAG of Cells.**
- **main = the maximal set of mutually-green Cells, materialized as a content-addressed
  snapshot, re-stamped whenever the set changes.** The DAG is the structure; main is a
  materialized view.
- Context objects are large and ~90% overlapping between sibling Cells → must be
  content-addressed and chunk-deduplicated (open problem, see §5).

## 3. The normalizer ("semantic gofmt")

Normalization = **picking a canonical representative from an equivalence class of programs.**
The design knob is *which equivalence relation*. Coarser = more conflicts killed, but more
risk of false-equivalence and eventual undecidability. It is a **tower of quotient
operations with a decidability cliff.**

| Layer | Collapses | Decidable? | Mechanism |
|---|---|---|---|
| 0 — Lexical | whitespace, quotes, semicolons | trivially | pretty-print |
| 1 — Desugaring | ternary↔if, arrow↔fn-decl, loop forms | yes | elaborate to core calculus (the IR) |
| 2 — Naming (α-equiv) | local var names | yes | De Bruijn indices; **names → metadata, not discarded** |
| 3 — Ordering | imports, independent decls, commutative ops | yes, *conservative* | dependency/effect analysis to prove independence |
| 4 — Simplification | constant fold, dead branch, unused binding | yes for a fixed rule set | algebraic rewrite |
| ═══ DECIDABILITY CLIFF (Rice's theorem) ═══ ||||
| 5 — Behavioral equiv | quicksort vs bubblesort | **undecidable** | **model guesses, contract verifies** |

### Hard rules

- **The normalizer must be a coward.** Surface forms that *look* interchangeable often aren't
  (`forEach` ≠ `for` for break/return/closure; float `+` not commutative; params can't reorder;
  object-key order is observable). When independence can't be *proven*, **bail** — a surviving
  spurious conflict is acceptable; a silent miscompile is catastrophic.
- **Confluence is non-negotiable.** The rewrite system must be confluent and terminating
  (Church–Rosser) or two equivalent inputs hash differently and the conflict reappears
  silently. Keep the rule set small; aggressiveness fights confluence.
- **Identity vs presentation.** Structure → the hash. Names + style → presentation metadata
  (Unison's move). Render to a single house style. Normalization destroys exactly the residue
  that's *noise* (formatting, loop-form, order) and preserves exactly the residue that's
  *signal* (it survives as a different hash). Variable names are the one style-shaped,
  signal-carrying exception → metadata, not discarded, not hashed.
- **Above the cliff: model proposes, contract disposes.** The model is a heuristic equivalence
  oracle; every guess is checked by execution. It is never authoritative.
- **The contract suite guards the normalizer itself.** Any rewrite that makes a green
  realization fail is, by definition, an unsound rewrite → rejected.

No cross-language identity in v1 (different semantics → a layer-5 problem in disguise).

## 4. The reconciler — the engine

### Master concept: auto-merge iff commute

> Two changes integrate without intervention **iff they commute** — A-then-B yields the same
> canonical state as B-then-A.

This is Pijul/Darcs patch theory (merge = pushout of commuting patches). The normalizer and
reconciler compute the *same* property at different scales: the normalizer asks "can I reorder
these statements?" within a definition; the reconciler asks "can these sessions compose?"
across definitions. **Same independence/effect analysis (layer 3) drives both.**

### The problem that makes it a queue, not a merge

> **green(A) ∧ green(B) ⇏ green(A ∪ B).**

Each session passes its own contracts; the union can fail. So per-session green is never
trusted — the reconciler re-runs contracts on the *composed* state. This is a **merge queue
with speculative gating** (cf. Bors / GitHub merge queue / Zuul), generalized from a linear
PR queue to continuous N-way composition.

### The tiered classifier

Every incoming Cell is classified against main + in-flight sessions:

| Tier | Relationship | Test | Action | Cost |
|---|---|---|---|---|
| 0 — Disjoint | touches definitions nothing else touches | commutes trivially | **admit immediately**; run affected contracts' dep-closure | ~free |
| 1 — Independent overlap | same module, layer-3 proves no dependency | commutes (proven) | mechanical compose; run affected contracts | cheap |
| 2 — Non-commuting, mergeable | same definition, model can propose a union realization | model proposes, contract disposes | re-realize region; run *union* of contracts; admit iff green | model + tests |
| 3 — Semantic contradiction | contracts conflict / no realization passes both | unmergeable | **quarantine; escalate at the intent level** | human |

The bet: **the vast majority of concurrent agent work is Tier 0.** Conflicts are rare and
precisely localized; the classifier *proves* which, instead of git's "every adjacent line is a
maybe-conflict."

### Why it's affordable

1. **Commutativity parallelizes the queue.** Tier 0/1 can't interfere → admit in parallel, no
   joint test. Only contended definitions (Tier 2) serialize. Cost scales with *contention*,
   not change throughput.
2. **Content-addressing → exact incremental testing.** A definition's hash didn't change → its
   contracts are still valid → skip them (exact, not heuristic). Continuous reconciliation is
   only affordable because most contracts are provably still-green.

### Guarantees for `main`

- **Always green** — every member Cell's contracts (incl. union checks) pass. main is never
  broken; this replaces "review."
- **Reproducible** — each restamp is a content-addressed pin; any past main is exactly
  rebuildable (answers the regulator / deploy / 3am-debugger demand).
- **Progress** — green work integrates with no human action, and no session is starved.

### The trust ceiling

The reconciler always runs **(agent contracts ∪ human spec invariants ∪ global invariants)**.
Agents may *add* contracts but cannot *escape* the human-authored ceiling. This is where
"judgment moved from reviewing diffs to authoring specs" cashes out.

### The human experience

A conflict reaches a human **only when two intents genuinely contradict** — i.e. when people
actually disagreed about what the system should do. Everything git makes you resolve by hand
(adjacent lines, import order, stale rebases) is here either proven-commuting or
auto-re-realized.

## 5. Open problems (in rough priority)

1. **Livelock / fairness under a moving main.** Tier-2 sessions re-realize against a moving
   target and may never land. Need a fairness mechanism (freeze target snapshot → landing slot
   → fast-forward). The genuinely unsolved hard problem; merge-queue batching/bisection is the
   starting point but the continuous N-way case is harder.
2. **Flaky contracts are poison.** An automated gate cannot tolerate non-deterministic
   contracts. Flake detection/quarantine is first-class infrastructure.
3. **The re-realization boundary.** When exactly to regenerate vs. trust the pin. Too eager →
   non-reproducible main; never → intent decays into decoration. Default: regenerate *only* on
   a Tier-2 collision, scoped to the contested region, contract-gated.
4. **Context storage.** Contexts are huge and mostly-overlapping; need content-addressed chunk
   dedup / prompt-as-pointer-into-a-corpus, or it's a disk-space bomb.
5. **Contract completeness / gaming.** Weak agent-authored contracts admit garbage; the
   human-authored spec ceiling is the mitigation but spec authoring becomes the bottleneck.

## 6. Prior art

- **Unison** — content-addressed AST as code identity; names as metadata; no builds. Closest
  existing thing to layer 2. Cautionary: invented language + VCS together → slow adoption.
- **Pijul / Darcs** — patch theory; commutation of changes; merge as pushout. Backbone of the
  classifier.
- **Merge queues (Bors / GitHub / Zuul)** — speculative gating for green-alone ≠ green-together.
- **Content-addressed build systems (Bazel/Buck)** — incremental selection; here *exact*
  because identity = content hash.
- **CRDTs** — for the genuinely commutative substrate.
- **MPS / projectional editing** — text as a projection of structure.
- **Spec-driven development (spec-kit, Kiro)** — the human-authored top layer; this VCS is the
  substrate that stops specs from rotting.

## 7. Prototype plan

The riskiest foundational claim is that **normalization can fold LLM stylistic entropy down to
one hash** — everything (content-addressing, Tier-0 auto-merge, incremental testing) collapses
if it can't. So the first falsifiable prototype of braid is the **normalizer (layers 0–2) on Python**
(`ast`, zero deps), measured by:

- **Recall:** stylistic variants of one function → **1 hash** (entropy folded).
- **Precision:** semantically different functions → **distinct hashes** (no false collisions).

### Prototype status (built, TDD, zero-dependency)

- `normalizer.py` — layers 0–2 + `free_names()` (layer-3 dependency extraction), scope-accurate.
- `reconciler.py` — commutativity classifier (Tiers 0/1/2) + minimal merge; stylistic edits
  are no-ops.
- `contracts.py` — materialize a composed codebase and run contracts on it.
- The reconciler **gates on contract execution**: a session is admitted only if the union of
  accumulated + session contracts stays green on the composed state — demonstrating
  `green(A) ∧ green(B) ⇏ green(A∪B)` (`demo_contracts.py`). Contracts accumulate as the spec
  ceiling; a red union escalates (Tier 3) and main stays green.
- `merge.py` + reconciler — **Tier 2 "model proposes, contract disposes"**: on a same-def
  structural conflict, a dependency-injected `proposer` suggests a merge, admitted only if it
  passes the contract gate (`demo_merge.py`: competent model → auto-merged, careless model →
  escalated). The proposer is the seam where a real Claude call plugs in (`make_llm_proposer`).
- Tier constants now match this doc: 0 disjoint, 1 dep-coupled, 2 model-merged, 3 escalated.
- `provenance.py` — **requirement 1**: a content-addressed chunk store (dedups the ~90%
  overlapping sibling contexts — the §5#4 disk-space bomb), a `Context` (intent/prompt/files/
  conversation/model/params) → manifest of chunk hashes, and a `CellLog` linking each
  definition's realization hash to its generating context. Provenance is keyed by *meaning*
  (the normalized hash), so a stylistic variant resolves to the same cell. The reconciler fires
  an `on_admit` hook so provenance is recorded only for admitted work (`demo_provenance.py`:
  point at a line of main → recover the prompt/files/model that produced it; 3× dedup at 3
  agents, scaling with fleet size).
- `fairness.py` — **§5#1 livelock/fairness**, as a deterministic discrete-round simulation.
  The optimistic (no-coordination) policy *starves* a slow session on a hot definition (it
  re-realizes against a moving main forever and never lands). A per-contended-def **lease +
  aging** policy freezes the target so the holder lands in one attempt with zero wasted
  re-realization, and aging guarantees progress (no starvation). Crucially leases engage ONLY
  on contended defs — disjoint (Tier-0) work is coordination-free under both policies
  (`demo_fairness.py`: optimistic → BIG never lands, 12 wasted; leased → BIG lands round 3, 1
  attempt, 0 wasted).
- `live.py` — **the unified engine**. `reconciler.integrate()` is the single shared decision
  (extracted so batch and live agree); `LiveReconciler` drives sessions that arrive over time
  and take `cost` rounds to realize, applies the lease+aging discipline on contended defs, runs
  the real contract/merge gate at land time, and records provenance on admit. Disjoint work is
  coordination-free; contended work is leased so the slow session lands in one attempt; genuine
  contradictions escalate; main stays green throughout (`demo_live.py`: a mixed workload —
  slow+fast flag agents merged in sequence, a disjoint version bump, a rogue contradiction
  escalated, 3.9× context dedup). `use_leases=False` reproduces the optimistic starvation.
- `repo.py` + `cli.py` + `braid` — **a usable, multi-file CLI prototype**. A `.braid/` store
  over a Python file *or directory*: `braid init / submit / diff / abandon /
  reconcile [--apply] / status / show / blame / log`. Units are keyed `path::name` (so the same
  function name in different files never collides); each file's `import` preamble is carried as a
  materialized unit. A session is a set of file edits relative to main (a single file via `--as`,
  or a whole edited tree); reconcile splits merged units back per file and writes each changed
  file, records who/what produced each def, and keeps `main` green; conflicts stay pending.
  See `README.md`. (Cross-file dependency *tiers* degrade to Tier-0 — `free_names` returns real
  names, not `path::name` — cosmetic, since both auto-merge.)
- `llm.py` — **the real-model seam**, and the only file in braid that talks to a network. The
  Anthropic SDK is imported lazily inside `make_call_model`, so the rest of braid and the whole
  test suite stay standard-library-only and offline (`test_importing_llm_does_not_import_the_sdk`
  pins this). `make_merge_proposer` backs the Tier-2 seam; `make_llm_realizer` backs rebuild.
  Both validate that the model returned exactly one definition with the requested name, and both
  treat a refusal as "no proposal" — which escalates, the correct safe default for a gate.
  `repo.reconcile(proposer=...)` now threads the proposer through, so Tier 2 is reachable from
  the CLI (`braid reconcile --propose`) rather than only from the engine.
- `repo.rebuild` + `braid rebuild` — **regeneration checked against the pin**. `main.json` is the
  lockfile holding each unit's pinned realization; rebuild regenerates every definition from its
  recorded intent and compares by normalized hash. The target's own realization is stripped from
  the context first, so the model cannot read back the answer it is being asked to reproduce.
  Three buckets: identical (same meaning), divergent (a different realization of the same intent
  — §0's residual decisions, reported and contract-checked, never hidden), and missing (no
  recorded intent). `--apply` restores from the pin, not from the regenerated source: the lock
  stays authoritative and the regeneration is the verification.
- `demo_braid.py` — the capstone: the stylistic no-op, eight concurrent agents (7 land
  unattended, one Tier-2 model-merge, one genuine contradiction escalated in English), `blame`
  recovering a prompt from a shipped line, and the teardown/rebuild encore.
- Tests: normalizer/reconciler/contracts/merge (7 each) + provenance (8) + fairness (5) +
  live (5) + repo (14) + rebuild (9) + llm (9) = 78 across 10 suites; 7 demos.

Not yet built (DESIGN.md §5): flake quarantine; exact incremental test selection; richer
normalization (import sorting, statement commutativity, cross-file dependency tiers, and
desugaring — `x += 1` and `x = x + 1` still hash differently, as do a ternary and its `if`);
agents that actually re-realize (the live sim uses fixed per-session variants).
