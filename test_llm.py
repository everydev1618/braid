"""Zero-dependency TDD spec for the real-model seam (llm.py).

llm.py is the only file in braid that talks to a network. These tests never do: they drive the
seam with a fake `call_model`, which is the whole point of the dependency injection. The first
test pins the property that keeps `python3 test_*.py` runnable with nothing installed --
importing llm must not import the Anthropic SDK.

Run: python3 test_llm.py
"""

import sys
import traceback

from llm import LLMError, build_realize_prompt, make_llm_realizer
from provenance import Context

CTX = Context(
    intent="make checkout idempotent",
    prompt="make checkout idempotent",
    files={"cart.py": "def total(items):\n    return sum(i.price for i in items)\n"},
    messages=[],
    model="claude-opus-5",
    params={},
)
CONTRACTS = [("idempotent", "assert checkout(c) == checkout(checkout(c))")]


def _fake(reply):
    calls = []

    def call_model(prompt):
        calls.append(prompt)
        return reply
    return call_model, calls


# --- the zero-dependency guarantee ---------------------------------------

def test_importing_llm_does_not_import_the_sdk():
    """The suite must run on a machine with nothing installed; the SDK loads on first call."""
    assert "anthropic" not in sys.modules, "llm.py must import the SDK lazily"


# --- the prompt ----------------------------------------------------------

def test_realize_prompt_carries_intent_context_and_contracts():
    p = build_realize_prompt("checkout.py::checkout", CTX, CONTRACTS)
    assert "make checkout idempotent" in p
    assert "checkout(c) == checkout(checkout(c))" in p, "the model should aim at the contracts"
    assert "def total(items)" in p, "sibling context should be available"
    assert "checkout" in p


def test_realize_prompt_asks_for_exactly_one_definition():
    p = build_realize_prompt("checkout.py::checkout", CTX, CONTRACTS)
    low = p.lower()
    assert "one" in low or "single" in low
    assert "checkout.py::checkout" in p or "`checkout`" in p


# --- extraction ----------------------------------------------------------

def test_realizer_extracts_fenced_code():
    call, calls = _fake("Sure!\n```python\ndef checkout(c):\n    return sorted(set(c))\n```\nDone.")
    src = make_llm_realizer(call)("checkout.py::checkout", CTX, CONTRACTS)
    assert src.startswith("def checkout(c):")
    assert "Sure!" not in src and "Done." not in src
    assert len(calls) == 1


def test_realizer_accepts_unfenced_code():
    call, _ = _fake("def checkout(c):\n    return sorted(set(c))\n")
    src = make_llm_realizer(call)("checkout.py::checkout", CTX, CONTRACTS)
    assert src.startswith("def checkout(c):")


def test_realizer_accepts_a_class_definition():
    call, _ = _fake("```python\nclass Cart:\n    def add(self, x):\n        pass\n```")
    src = make_llm_realizer(call)("cart.py::Cart", CTX, CONTRACTS)
    assert src.startswith("class Cart:")


# --- failing loudly rather than silently diverging ------------------------

def test_realizer_rejects_an_empty_reply():
    call, _ = _fake("")
    try:
        make_llm_realizer(call)("checkout.py::checkout", CTX, CONTRACTS)
    except LLMError:
        return
    raise AssertionError("an empty reply (or refusal) must raise, not return junk")


def test_realizer_rejects_prose_that_is_not_code():
    call, _ = _fake("I think you should refactor the cart module first.")
    try:
        make_llm_realizer(call)("checkout.py::checkout", CTX, CONTRACTS)
    except LLMError:
        return
    raise AssertionError("non-parsing output must raise")


def test_realizer_rejects_a_definition_with_the_wrong_name():
    call, _ = _fake("```python\ndef total(c):\n    return 0\n```")
    try:
        make_llm_realizer(call)("checkout.py::checkout", CTX, CONTRACTS)
    except LLMError:
        return
    raise AssertionError("the model must return the definition it was asked for")


def main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception:
            failed += 1
            print(f"  FAIL  {t.__name__}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
