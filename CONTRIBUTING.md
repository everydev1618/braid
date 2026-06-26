# Contributing to braid

braid is an early, clean-slate prototype of a version control system for the agentic age. It
explores ideas more than it ships a product — so the bar for contributions is *clarity and
falsifiability*: a change should make an idea sharper, demonstrate it, or prove it wrong.

Please read [DESIGN.md](DESIGN.md) first. It is the source of truth for the architecture and
the rationale; code is downstream of it.

## Setup

No dependencies. You need **Python 3.10+** and nothing else — no virtualenv, no package
install. (CI-grade tools like `pytest`/`ruff` are optional; the suite runs without them.)

```sh
git clone git@github.com:everydev1618/braid.git
cd braid
python3 -m pytest -q      # if you have pytest...
# ...or, with zero dependencies:
for t in test_*.py; do python3 "$t"; done
```

## Running things

```sh
python3 test_normalizer.py        # any individual suite
python3 demo_live.py              # any demo (they are runnable and self-explanatory)
python3 cli.py --help            # the CLI (or ./braid once on your PATH)
```

## How the code is laid out

| file | responsibility |
|---|---|
| `normalizer.py` | canonical content hashing + free-name (dependency) analysis |
| `reconciler.py` | commutativity classifier + contract-gated `integrate`/`reconcile` |
| `contracts.py` | materialize a composed codebase and run executable contracts |
| `merge.py` | the Tier-2 model-proposer seam |
| `provenance.py` | content-addressed context store + cell log |
| `fairness.py` | livelock simulation + lease/aging policy |
| `live.py` | the unified engine (scheduling + leasing + gating + provenance) |
| `repo.py` / `cli.py` / `braid` | on-disk store, CLI, launcher |
| `test_*.py` | one suite per module |
| `demo_*.py` | one runnable demonstration per capability |

## Working style

- **Test-driven.** Write the test first, watch it fail, then make it pass. Every module has a
  `test_*.py`; add to it (or add a new one) before changing behavior.
- **Zero dependencies.** Standard library only. Tests are plain `python3 file.py` runnable so
  anyone can run them anywhere; don't introduce a framework requirement.
- **Match the surrounding code.** Comment density, naming, and idiom should look like the file
  you're editing. Prefer a small, confluent, *cowardly* change over a clever one — see the
  normalizer's "be a coward" rule in DESIGN.md §3.
- **Demonstrate it.** A new capability should come with a `demo_*.py` that makes it obvious,
  and the claim it validates (or refutes) stated plainly.
- **Keep DESIGN.md honest.** If you build or disprove something in §5 (open problems), update
  that section. The doc should never overstate what the code does.

## Good first contributions

These are concrete gaps called out in DESIGN.md §5:

- **Multi-file repos** — `repo.py` tracks a single module today; generalize to a directory.
- **Richer normalization** — import sorting and statement-level commutativity (layers 3–4),
  each with a confluence argument and tests.
- **Real model wiring** — turn `make_llm_proposer` into an actual API call behind the existing
  seam, so Tier-2 conflicts auto-resolve.
- **Flake quarantine** — detect non-deterministic contracts so the gate stays trustworthy.

## Commits & PRs

- Keep commits focused; explain the *why* in the message.
- Make sure every `test_*.py` passes and `python3 -m py_compile *.py` is clean before opening a PR.
- Describe what the change demonstrates or fixes, and link the relevant DESIGN.md section.
