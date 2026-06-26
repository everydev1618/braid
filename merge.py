"""merge.py -- the Tier-2 "model proposes, contract disposes" seam (DESIGN.md s.4).

When two sessions change the SAME definition different ways, the changes do not commute, so
the reconciler cannot merge them mechanically. It hands the conflict to a *proposer* (an LLM
in production) which suggests a merged definition. Crucially the proposer is NEVER trusted:
its suggestion is admitted only if it passes the contract gate. The model proposes; the
contract disposes.

The proposer is dependency-injected (`Callable[[MergeRequest], str | None]`) so the harness is
testable with deterministic stubs and wireable to a real model later. `make_llm_proposer`
adapts any `call_model(prompt) -> str` into a proposer; no network is required here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class MergeRequest:
    name: str            # the definition in conflict
    base: str | None     # the common-ancestor source (may be None if newly added by both)
    ours: str            # the already-integrated version (what main currently holds)
    theirs: str          # the incoming session's version
    codebase: dict       # the rest of current main (for context)
    contracts: list      # the contracts the merge must satisfy (the proposer should aim at these)


def build_merge_prompt(req: MergeRequest) -> str:
    """The prompt a real model would receive. Returned as text so it is inspectable/testable."""
    contract_src = "\n".join(src for _, src in req.contracts) or "(none)"
    return (
        f"Two agents changed `{req.name}` in incompatible ways. Produce ONE merged "
        f"definition that preserves the intent of both and satisfies every contract.\n\n"
        f"# BASE\n{req.base or '(new definition)'}\n\n"
        f"# OURS (already in main)\n{req.ours}\n\n"
        f"# THEIRS (incoming)\n{req.theirs}\n\n"
        f"# CONTRACTS THE RESULT MUST SATISFY\n{contract_src}\n\n"
        f"Return only the merged Python definition of `{req.name}`."
    )


def make_llm_proposer(call_model):
    """Adapt a `call_model(prompt) -> str` into a proposer that extracts the code it returns."""
    def proposer(req: MergeRequest):
        reply = call_model(build_merge_prompt(req))
        if not reply:
            return None
        return _extract_code(reply)
    return proposer


_FENCE = re.compile(r"```(?:python)?\n(.*?)```", re.DOTALL)


def _extract_code(reply: str) -> str:
    m = _FENCE.search(reply)
    return (m.group(1) if m else reply).strip() + "\n"
