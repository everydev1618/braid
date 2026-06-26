"""Zero-dependency TDD spec for the normalizer (layers 0-2).

Run: python3 test_normalizer.py

The claim under test: normalization folds LLM stylistic entropy down to ONE hash.
  - Recall: stylistically-different-but-semantically-identical code -> SAME hash.
  - Precision: semantically-different code -> DISTINCT hashes (no false collisions).
  - Conservatism: forms that only LOOK equivalent (operand order on `+`) are NOT
    collapsed, because the normalizer must be a coward.
"""

import sys
import traceback

from normalizer import normalize, normalize_hash


# --- Equivalence classes: every member must share one hash (recall) ---------

EQUIV_CLASSES = {
    "add_two_params": [
        "def f(a, b):\n    return a + b\n",
        "def f(x, y):\n    return x + y\n",            # alpha-rename of params
        "def g(foo, bar):\n    return foo + bar\n",    # def name is metadata too
        "def f(a, b):\n    return (a + b)\n",          # redundant parens (layer 0)
        "def f(a, b):\n\n    return a + b\n",          # blank line (layer 0)
    ],
    "local_temp": [
        "def f(n):\n    t = n * 2\n    return t\n",
        "def f(n):\n    result = n * 2\n    return result\n",
        "def compute(value):\n    doubled = value * 2\n    return doubled\n",
    ],
    "loop_sum": [
        "def f(xs):\n    total = 0\n    for x in xs:\n        total = total + x\n    return total\n",
        "def f(items):\n    acc = 0\n    for it in items:\n        acc = acc + it\n    return acc\n",
    ],
}


# --- Distinct programs: each must hash differently from all others (precision)

DISTINCT = {
    "subtract": "def f(a, b):\n    return a - b\n",
    "operand_order": "def f(a, b):\n    return b + a\n",   # conservative: NOT == add_two_params
    "const_three": "def f(n):\n    return n * 3\n",
    "no_temp_double": "def f(n):\n    return n * 2\n",      # structurally != local_temp
    "extra_term": "def f(a, b):\n    return a + b + 1\n",
}


def test_equivalence_classes_collapse_to_one_hash():
    for class_name, srcs in EQUIV_CLASSES.items():
        hashes = {normalize_hash(src) for src in srcs}
        assert len(hashes) == 1, f"{class_name}: expected 1 hash, got {len(hashes)}"


def test_all_classes_and_distinct_are_mutually_unique():
    reps = {name: normalize_hash(srcs[0]) for name, srcs in EQUIV_CLASSES.items()}
    reps.update({name: normalize_hash(src) for name, src in DISTINCT.items()})
    seen = {}
    for name, h in reps.items():
        assert h not in seen, f"false collision: {name} == {seen.get(h)}"
        seen[h] = name


def test_operand_order_is_not_collapsed():
    # a + b and b + a must differ (float + is not commutative; be a coward).
    assert normalize_hash("def f(a, b):\n    return a + b\n") != \
           normalize_hash("def f(a, b):\n    return b + a\n")


def test_original_names_kept_as_metadata():
    # Identity is the canonical structure; names survive as presentation metadata.
    result = normalize("def f(a, b):\n    return a + b\n")
    assert "a" in result.names.values()
    assert "b" in result.names.values()
    assert all(canon.startswith("v") for canon in result.names.keys())


def test_free_names_are_not_renamed():
    # builtins / globals are free identifiers and must be preserved verbatim.
    h1 = normalize_hash("def f(xs):\n    return len(xs)\n")
    h2 = normalize_hash("def f(ys):\n    return len(ys)\n")
    h3 = normalize_hash("def f(xs):\n    return sum(xs)\n")
    assert h1 == h2          # param renamed away
    assert h1 != h3          # len vs sum is real signal, preserved


def test_local_name_does_not_capture_free_name_in_sibling_scope():
    # f has a LOCAL `data`; g references a FREE (global) `data`. They are different
    # entities. Renaming f's local must not touch g's free reference.
    a = "def f(x):\n    data = x\n    return data\n\ndef g():\n    return data\n"
    b = "def f(x):\n    tmp = x\n    return tmp\n\ndef g():\n    return data\n"
    assert normalize_hash(a) == normalize_hash(b)


def test_shadowing_local_distinct_from_free_global():
    # Same idea, no-arg functions: changing only f's local must not alter g's free `n`.
    a = "def f():\n    n = 1\n    return n\n\ndef g():\n    return n + 1\n"
    b = "def f():\n    m = 1\n    return m\n\ndef g():\n    return n + 1\n"
    assert normalize_hash(a) == normalize_hash(b)


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
