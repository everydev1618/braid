"""experiment.py -- quantify the foundational claim.

Recall:    stylistic variants of one function should fold to exactly 1 hash.
Precision: a corpus of distinct functions should produce 0 false collisions.
Boundary:  algorithmically-equivalent-but-structurally-different code (the layer-5
           decidability cliff) is *expected* to differ -- the normalizer does NOT
           and must not claim behavioral equivalence. Reported honestly.

Run: python3 experiment.py
"""

from normalizer import normalize_hash


# Each family = stylistic variants that MUST collapse to one hash (recall).
RECALL_FAMILIES = {
    "add": [
        "def f(a, b):\n    return a + b\n",
        "def f(x, y):\n    return x + y\n",
        "def add(lhs, rhs):\n    return (lhs + rhs)\n",
        "def f(a, b):\n\n\n    return a + b  # sum\n",
    ],
    "clamp": [
        "def f(v, lo, hi):\n    if v < lo:\n        return lo\n    if v > hi:\n        return hi\n    return v\n",
        "def clamp(value, low, high):\n    if value < low:\n        return low\n    if value > high:\n        return high\n    return value\n",
    ],
    "count_evens": [
        "def f(xs):\n    c = 0\n    for x in xs:\n        if x % 2 == 0:\n            c = c + 1\n    return c\n",
        "def count(items):\n    total = 0\n    for it in items:\n        if it % 2 == 0:\n            total = total + 1\n    return total\n",
    ],
    "greet": [
        "def f(name):\n    return 'hi ' + name\n",
        "def greet(person):\n    return 'hi ' + person\n",
    ],
}

# Distinct functions that MUST NOT collide (precision).
PRECISION_CORPUS = {
    "add": "def f(a, b):\n    return a + b\n",
    "sub": "def f(a, b):\n    return a - b\n",
    "mul": "def f(a, b):\n    return a * b\n",
    "operand_order": "def f(a, b):\n    return b + a\n",
    "add_one": "def f(a, b):\n    return a + b + 1\n",
    "clamp": "def f(v, lo, hi):\n    if v < lo:\n        return lo\n    if v > hi:\n        return hi\n    return v\n",
    "count_evens": "def f(xs):\n    c = 0\n    for x in xs:\n        if x % 2 == 0:\n            c = c + 1\n    return c\n",
    "count_odds": "def f(xs):\n    c = 0\n    for x in xs:\n        if x % 2 == 1:\n            c = c + 1\n    return c\n",
    "greet": "def f(name):\n    return 'hi ' + name\n",
    "greet_bye": "def f(name):\n    return 'bye ' + name\n",
}

# The decidability cliff: same behavior, different structure -> EXPECTED to differ.
BOUNDARY_PAIRS = {
    "sum: loop vs builtin": (
        "def f(xs):\n    t = 0\n    for x in xs:\n        t = t + x\n    return t\n",
        "def f(xs):\n    return sum(xs)\n",
    ),
}


def main() -> int:
    print("=" * 64)
    print("RECALL  -- variants of one function should fold to 1 hash")
    print("=" * 64)
    recall_ok = True
    for fam, variants in RECALL_FAMILIES.items():
        hashes = {normalize_hash(s) for s in variants}
        ok = len(hashes) == 1
        recall_ok &= ok
        print(f"  [{'OK ' if ok else 'BAD'}] {fam:<14} {len(variants)} variants -> {len(hashes)} hash(es)")

    print("\n" + "=" * 64)
    print("PRECISION -- distinct functions should never collide")
    print("=" * 64)
    by_hash: dict[str, list[str]] = {}
    for name, src in PRECISION_CORPUS.items():
        by_hash.setdefault(normalize_hash(src), []).append(name)
    collisions = [v for v in by_hash.values() if len(v) > 1]
    precision_ok = not collisions
    print(f"  {len(PRECISION_CORPUS)} functions -> {len(by_hash)} distinct hashes")
    if collisions:
        for group in collisions:
            print(f"  [BAD] false collision: {' == '.join(group)}")
    else:
        print("  [OK ] zero false collisions")

    print("\n" + "=" * 64)
    print("BOUNDARY -- layer-5 (behavioral) equivalence is OUT OF SCOPE")
    print("=" * 64)
    for label, (a, b) in BOUNDARY_PAIRS.items():
        differ = normalize_hash(a) != normalize_hash(b)
        # Differing here is correct: proving these equal is undecidable (Rice);
        # that job belongs to the reconciler's model-proposes/contract-disposes tier.
        print(f"  [{'OK ' if differ else '!! '}] {label}: {'differ (correct)' if differ else 'collapsed (unsound!)'}")

    print("\n" + "-" * 64)
    verdict = recall_ok and precision_ok
    print(f"VERDICT: {'thesis holds for layers 0-2' if verdict else 'FAILED'} "
          f"(recall={'pass' if recall_ok else 'fail'}, precision={'pass' if precision_ok else 'fail'})")
    return 0 if verdict else 1


if __name__ == "__main__":
    raise SystemExit(main())
