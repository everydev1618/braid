"""reconciler.py -- braid's commutativity classifier + minimal merge engine (DESIGN.md s.4).

Model (deliberately small): a *codebase* is {definition_name: source}. A *session* hands back
a whole modified codebase. The reconciler folds sessions into a single green `main`, deciding
per session whether it commutes with what is already integrated.

The master rule: two changes auto-merge iff they commute. We decide commutativity at
definition granularity using two layer-3 facts:
  - content hash per definition (via the normalizer) -> what actually changed (stylistic
    edits hash equal, so they are NO-OPS, not conflicts), and
  - dependency edges per definition (free_names ∩ codebase) -> whether disjoint changes are
    nonetheless coupled.

Tiers:
  TIER0_DISJOINT  touched sets disjoint AND no dependency edge between them -> auto-merge.
  TIER1_DEP       touched sets disjoint but dependency-coupled -> auto-merge (union gated).
  TIER2_MERGED    same definition changed two ways -> proposer suggests a merge, admitted iff
                  it passes the contract gate ("model proposes, contract disposes").
  TIER3_CONFLICT  no proposer / proposer gives up / every candidate stays red, OR a union goes
                  red -> escalate to a human; main keeps the already-green version.

The contract gate runs on the COMPOSED state, so this catches green(A) and green(B) but
red(A union B). Not yet here (DESIGN.md s.5): livelock/fairness under a moving main, flake
quarantine, exact incremental test selection.
"""

from __future__ import annotations

from dataclasses import dataclass

from contracts import run_contracts
from merge import MergeRequest
from normalizer import free_names, normalize_hash

TIER0_DISJOINT = 0    # disjoint, no dependency edge -> auto-merge
TIER1_DEP = 1         # disjoint but dependency-coupled -> auto-merge + flag
TIER2_MERGED = 2      # non-commuting overlap, model proposed, contract accepted -> auto-merge
TIER3_CONFLICT = 3    # escalated to a human (no proposal, or every candidate stays red)

Codebase = dict  # {name: source}


@dataclass
class Change:
    touched: set       # names whose normalized hash differs from base (added/modified/removed)
    hashes: dict       # name -> new hash (None if the name was removed)


@dataclass
class ReconcileResult:
    merged: Codebase
    status: dict                       # session_id -> (tier, detail)
    conflicts: list                    # [(session_id, {conflicting names})]


def _hashes(cb: Codebase) -> dict:
    return {name: normalize_hash(src) for name, src in cb.items()}


def _deps(cb: Codebase) -> dict:
    names = set(cb)
    return {name: free_names(src) & names for name, src in cb.items()}


def changeset(base: Codebase, variant: Codebase) -> Change:
    bh, vh = _hashes(base), _hashes(variant)
    touched, new = set(), {}
    for name in set(bh) | set(vh):
        b, v = bh.get(name), vh.get(name)
        if b != v:
            touched.add(name)
            new[name] = v          # None when removed
    return Change(touched, new)


def _apply(current: Codebase, variant: Codebase, change: Change) -> Codebase:
    merged = dict(current)
    for name in change.touched:
        if name in variant:
            merged[name] = variant[name]    # added or modified
        else:
            merged.pop(name, None)          # removed
    return merged


def _try_propose(current, base, variant, change, conflict_names, contracts, proposer):
    """Ask the proposer to merge each conflicting def; return the candidate codebase iff it
    passes the contract gate. Returns None if there is no proposer, it gives up, or the
    result stays red -- in which case the caller escalates."""
    if proposer is None:
        return None
    # Start from the session's full change (incl. its disjoint additions)...
    candidate = _apply(current, variant, change)
    # ...then replace each conflicting def with a model-proposed merge.
    for name in conflict_names:
        req = MergeRequest(
            name=name,
            base=base.get(name),
            ours=current.get(name),
            theirs=variant.get(name),
            codebase=current,
            contracts=contracts,
        )
        proposal = proposer(req)
        if not proposal:
            return None
        candidate[name] = proposal
    return candidate if not run_contracts(candidate, contracts) else None


@dataclass
class IntegrationState:
    """The accumulated state of `main` as sessions are folded in. Shared by the batch
    reconciler and the live (scheduled) reconciler so both make identical decisions."""
    base: Codebase
    current: Codebase
    acc_touched: set
    acc_hash: dict
    acc_contracts: list

    @classmethod
    def start(cls, base, base_contracts=()):
        return cls(dict(base), dict(base), set(), {}, list(base_contracts))


@dataclass
class Outcome:
    admitted: bool
    tier: int
    detail: str
    conflict_names: set | None
    change: Change


def _commit(state: IntegrationState, change: Change, new_current: Codebase, contracts: list) -> None:
    state.current = new_current
    state.acc_touched |= change.touched
    for n in change.touched:
        src = new_current.get(n)
        state.acc_hash[n] = normalize_hash(src) if src is not None else None
    state.acc_contracts = contracts


def integrate(state: IntegrationState, variant: Codebase, new_contracts=(), proposer=None) -> Outcome:
    """Try to fold one session's `variant` into `state`, mutating `state` on admit.

    Admitted only if it does not structurally conflict (or a proposer merges it past the
    contract gate) AND the union of accumulated + new contracts stays green on the composed
    state. This is the single decision shared by `reconcile` and the live reconciler.
    """
    change = changeset(state.base, variant)
    contracts = state.acc_contracts + list(new_contracts)

    # Structural conflict: a name this session changes was already integrated to a
    # *different* hash (same definition, two different ways).
    conflict_names = {
        n for n in (change.touched & state.acc_touched)
        if change.hashes.get(n) != state.acc_hash.get(n)
    }
    if conflict_names:
        resolved = _try_propose(state.current, state.base, variant, change,
                                conflict_names, contracts, proposer)
        if resolved is None:
            return Outcome(False, TIER3_CONFLICT,
                           f"structural conflict on {sorted(conflict_names)}", conflict_names, change)
        _commit(state, change, resolved, contracts)
        return Outcome(True, TIER2_MERGED,
                       f"model-merged {sorted(conflict_names)}, contract green", None, change)

    merged = _apply(state.current, variant, change)

    # Contract gate on the COMPOSED state -- catches green(A) and green(B) but red(A union B).
    failures = run_contracts(merged, contracts)
    if failures:
        failed_ids = {cid for cid, _ in failures}
        return Outcome(False, TIER3_CONFLICT,
                       f"contract failure {sorted(failed_ids)}: {failures[0][1]}", failed_ids, change)

    if not change.touched:
        tier, detail = TIER0_DISJOINT, "no-op (stylistic only)"
    elif change.touched & state.acc_touched:
        tier, detail = TIER1_DEP, "duplicate change (same hash)"
    else:
        dep = _deps(merged)
        coupled = (
            any(dep.get(d, set()) & state.acc_touched for d in change.touched)
            or any(dep.get(d, set()) & change.touched for d in state.acc_touched)
        )
        tier = TIER1_DEP if coupled else TIER0_DISJOINT
        detail = "dependency-coupled" if coupled else "disjoint"

    _commit(state, change, merged, contracts)
    return Outcome(True, tier, detail, None, change)


def reconcile(base: Codebase, sessions: list, base_contracts=(), proposer=None,
              on_admit=None) -> ReconcileResult:
    """Fold sessions (in list order) into a single green `main`.

    Each session is `(id, variant)` or `(id, variant, contracts)`. Contracts accumulate, so an
    admitted session's contracts become part of the spec ceiling for every later session. A
    structural conflict with a `proposer` is handed off (Tier 2) and admitted only if the merge
    passes the gate; otherwise it escalates (Tier 3). The model proposes, the contract disposes.
    """
    state = IntegrationState.start(base, base_contracts)
    status: dict = {}
    conflicts: list = []

    for item in sessions:
        sid, variant = item[0], item[1]
        new_contracts = list(item[2]) if len(item) > 2 else []
        out = integrate(state, variant, new_contracts, proposer)
        status[sid] = (out.tier, out.detail)
        if out.admitted:
            if on_admit:
                on_admit(sid, out.change, state.current)
        else:
            conflicts.append((sid, out.conflict_names))

    return ReconcileResult(state.current, status, conflicts)
