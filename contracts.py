"""contracts.py -- materialize a composed codebase and run executable contracts on it.

A contract is the thing that replaces code review (DESIGN.md s.4): executable acceptance
criteria. Here a contract is `(id, source)` where `source` is Python that runs with the
codebase's definitions in scope and raises on violation (typically `assert ...`).

`run_contracts` is what the reconciler calls to gate a composed state: a non-empty failure
list means the union is red and the session must not be admitted.

Prototype caveat: this `exec`s code in-process with no sandbox. Real braid runs contracts in
an isolated executor (the "speculative test execution" of the merge queue). Fine for the
prototype; do not point it at untrusted input.
"""

from __future__ import annotations

Codebase = dict   # {name: source}
Contract = tuple  # (id, source)


def materialize(codebase: Codebase) -> dict:
    """Exec all definitions into a fresh namespace and return it."""
    ns: dict = {}
    source = "\n\n".join(codebase[name] for name in codebase)
    exec(compile(source, "<braid-main>", "exec"), ns)
    return ns


def run_contracts(codebase: Codebase, contracts) -> list:
    """Return a list of (contract_id, error) for every failing contract; [] if all green."""
    try:
        ns = materialize(codebase)
    except Exception as e:  # the merged code doesn't even load
        return [("<materialize>", f"{type(e).__name__}: {e}")]

    failures = []
    for cid, csrc in contracts:
        try:
            exec(compile(csrc, f"<contract {cid}>", "exec"), dict(ns))
        except Exception as e:
            failures.append((cid, f"{type(e).__name__}: {e}"))
    return failures
