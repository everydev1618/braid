"""llm.py -- braid's only seam to a real model.

Two places in braid ask a model to produce code, and neither of them is authoritative:

  * the **Tier-2 proposer** (`merge.py`): two agents changed the same definition in
    incompatible ways; a model proposes a union realization and the contract gate decides
    whether it lands. "The model proposes, the contract disposes."
  * the **realizer** (`repo.rebuild`): regenerate a definition from its recorded intent so the
    result can be checked against the pinned realization hash.

Both are dependency-injected `call_model(prompt) -> str` seams, so the rest of braid -- and the
whole test suite -- stays standard-library-only and offline. The Anthropic SDK is imported
lazily inside `make_call_model`, never at module import, so `python3 test_*.py` runs on a
machine with nothing installed.

Requires `pip install anthropic` and credentials (`ANTHROPIC_API_KEY`, or `ant auth login`)
only when a call is actually made.
"""

from __future__ import annotations

import ast
import os

from merge import _extract_code, build_merge_prompt

DEFAULT_MODEL = "claude-opus-5"

SYSTEM = (
    "You are a code realizer inside braid, a version control system that treats intent as the "
    "source of truth and code as a build output. You are given an intent and its surrounding "
    "context and asked to produce the corresponding Python definition. Return code only."
)


class LLMError(Exception):
    pass


# --- the transport --------------------------------------------------------

def available() -> bool:
    """True if a real call could plausibly succeed (SDK installed and credentials present)."""
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    creds = os.path.expanduser("~/.config/anthropic/credentials")
    return os.path.isdir(creds) and bool(os.listdir(creds))


def make_call_model(model: str = DEFAULT_MODEL, max_tokens: int = 16000,
                    effort: str = "high", system: str = SYSTEM):
    """Return a `call_model(prompt) -> str` backed by a real Claude call.

    Streams (so a large `max_tokens` can't hit an HTTP timeout) and opts into server-side
    refusal fallbacks. A refusal returns "" rather than raising: for the proposer that means
    "no proposal", which escalates the conflict to a human -- the correct safe default for a
    gate. The SDK import happens here, not at module import.
    """
    import anthropic

    client = anthropic.Anthropic()

    def call_model(prompt: str) -> str:
        with client.beta.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            thinking={"type": "adaptive"},
            output_config={"effort": effort},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            message = stream.get_final_message()
        if message.stop_reason == "refusal":
            return ""
        return "".join(b.text for b in message.content if b.type == "text")

    return call_model


# --- the realizer ---------------------------------------------------------

def build_realize_prompt(unit: str, context, contracts) -> str:
    """The prompt that regenerates one definition from intent. Returned as text so it is
    inspectable and testable without a network."""
    _, name = unit.split("::", 1)
    siblings = "\n\n".join(
        f"# --- {path} ---\n{src.rstrip()}" for path, src in sorted(context.files.items()) if src.strip()
    ) or "(none)"
    contract_src = "\n".join(src for _, src in contracts) or "(none)"
    return (
        f"Write exactly one Python definition named `{name}` (unit `{unit}`).\n\n"
        f"## Intent\n{context.intent}\n\n"
        f"## Original request\n{context.prompt}\n\n"
        f"## Surrounding code (the rest of the codebase; `{name}` itself is deliberately absent)\n"
        f"{siblings}\n\n"
        f"## Contracts it must satisfy\n{contract_src}\n\n"
        "Return a single top-level `def` or `class` and nothing else: no prose, no imports "
        "(the module's imports are carried separately), no usage examples, no tests."
    )


def _one_definition(src: str, name: str) -> str:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise LLMError(f"model returned code that does not parse: {e}") from e
    defs = [n for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    if len(defs) != 1:
        raise LLMError(f"expected exactly one definition, got {len(defs)}")
    if defs[0].name != name:
        raise LLMError(f"expected a definition named `{name}`, got `{defs[0].name}`")
    return src


def make_llm_realizer(call_model=None):
    """Adapt a `call_model` into a `realize(unit, context, contracts) -> source` for rebuild."""
    call_model = call_model or make_call_model()

    def realize(unit: str, context, contracts) -> str:
        _, name = unit.split("::", 1)
        reply = call_model(build_realize_prompt(unit, context, contracts))
        if not reply or not reply.strip():
            raise LLMError(f"no realization returned for `{unit}` (empty reply or refusal)")
        return _one_definition(_extract_code(reply), name)

    return realize


def replay_realizer(sources):
    """A realizer that returns canned source per unit key -- the offline demo path."""
    def realize(unit, context, contracts):
        if unit not in sources:
            raise LLMError(f"no recorded realization for `{unit}`")
        return sources[unit]
    return realize


# --- the Tier-2 proposer --------------------------------------------------

def make_merge_proposer(call_model=None):
    """A real-model proposer for the Tier-2 seam in `merge.py`/`reconciler.py`.

    Returns None when the model declines or produces nothing usable, which the reconciler
    treats as "escalate" -- the gate stays authoritative either way.
    """
    call_model = call_model or make_call_model()

    def proposer(req):
        reply = call_model(build_merge_prompt(req))
        if not reply or not reply.strip():
            return None
        try:
            return _one_definition(_extract_code(reply), req.name)
        except LLMError:
            return None

    return proposer
