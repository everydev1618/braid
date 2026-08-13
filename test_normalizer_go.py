"""Zero-dependency TDD spec for the Go frontend's normalizer (layer 0).

Run: python3 test_normalizer_go.py

The claim under test is narrower than the Python normalizer's. Go normalization is a
*lexical* canonicalization: comments, whitespace, line breaks and redundant semicolons
fold to one hash. It does NOT alpha-rename (no scope analysis) and does not strip
redundant parens, so those still change the hash -- deliberately, per DESIGN.md's
"be a coward" rule: an unproven rewrite does not earn its place.
"""

import sys
import traceback

from normalizer_go import (
    free_names,
    normalize,
    normalize_hash,
    parse_module,
    render_module,
)

# --- Equivalence classes: every member must share one hash (recall) ---------

EQUIV_CLASSES = {
    "greeting": [
        'func greeting() string {\n\treturn "Hello, World!"\n}\n',
        'func greeting() string {\n\t// the canonical greeting\n\treturn "Hello, World!"\n}\n',
        'func greeting() string {\n\n\treturn "Hello, World!"\n\n}\n',
        'func greeting() string {\n    return "Hello, World!"\n}\n',      # spaces not tabs
        'func greeting() string { return "Hello, World!" }\n',            # one line
        '/* doc */ func greeting() string {\n\treturn "Hello, World!"\n}\n',
    ],
    "two_statements": [
        "func f() int {\n\ta := 1\n\tb := 2\n\treturn a + b\n}\n",
        "func f() int {\n\ta := 1; b := 2\n\treturn a + b\n}\n",          # explicit semicolons
        "func f() int {\n\ta := 1;\n\tb := 2;\n\treturn a + b;\n}\n",     # trailing semicolons
    ],
    "loop": [
        "func s(n int) int {\n\ttotal := 0\n\tfor i := 0; i < n; i++ {\n\t\ttotal += i\n\t}\n\treturn total\n}\n",
        "func s(n int) int {\n\ttotal := 0\n\t// sum them up\n\tfor i := 0; i < n; i++ {\n\t\ttotal += i // accumulate\n\t}\n\treturn total\n}\n",
    ],
}


def test_equivalence_classes_share_one_hash():
    for label, variants in EQUIV_CLASSES.items():
        hashes = {normalize_hash(v) for v in variants}
        assert len(hashes) == 1, f"{label}: {len(hashes)} hashes, expected 1"


def test_distinct_meanings_have_distinct_hashes():
    # precision: real differences must survive normalization
    a = 'func greeting() string {\n\treturn "Hello, World!"\n}\n'
    b = 'func greeting() string {\n\treturn "Goodbye, World!"\n}\n'
    c = 'func greeting() string {\n\treturn "Hello, World!" + "!"\n}\n'
    assert len({normalize_hash(x) for x in (a, b, c)}) == 3


def test_renaming_a_func_changes_the_hash():
    # Documented limitation vs the Python frontend: no alpha-renaming for Go, so a
    # renamed identifier is a real change, not presentation metadata.
    a = "func greeting() string {\n\treturn \"hi\"\n}\n"
    b = "func salutation() string {\n\treturn \"hi\"\n}\n"
    assert normalize_hash(a) != normalize_hash(b)
    assert normalize(a).names == {}


def test_comment_markers_inside_string_literals_survive():
    a = 'func f() string {\n\treturn "// not a comment"\n}\n'
    b = 'func f() string {\n\treturn ""\n}\n'
    assert normalize_hash(a) != normalize_hash(b)
    assert "// not a comment" in normalize(a).canonical


def test_raw_string_literals_keep_their_bytes():
    # a raw string's newlines and backslashes are content, not formatting
    a = "func f() string {\n\treturn `line1\nline2`\n}\n"
    b = "func f() string {\n\treturn `line1 line2`\n}\n"
    assert normalize_hash(a) != normalize_hash(b)


def test_rune_and_escaped_quote_do_not_desync_the_lexer():
    src = "func f() bool {\n\tq := '\\''\n\ts := \"a\\\"b\"\n\treturn q == '\\'' && s != \"\"\n}\n"
    assert normalize_hash(src) == normalize_hash(src + "\n// trailing\n")


def test_division_is_not_mistaken_for_a_comment():
    a = "func f(a int, b int) int {\n\treturn a / b\n}\n"
    b = "func f(a int, b int) int {\n\treturn a * b\n}\n"
    assert normalize_hash(a) != normalize_hash(b)


# --- structural split: preamble / defs -------------------------------------

MODULE = '''\
package main

import "fmt"

// greeting is the thing we say.
func greeting() string {
\treturn "Hello, World!"
}

type Greeter struct {
\tName string
}

func (g Greeter) Greet() string {
\treturn greeting() + " " + g.Name
}

const punctuation = "!"

var count = 0

func main() {
\tfmt.Println(greeting())
}
'''


def test_package_and_imports_are_the_preamble():
    preamble, order, defs = parse_module(MODULE)
    assert "package main" in preamble
    assert 'import "fmt"' in preamble
    assert "func greeting" not in preamble


def test_top_level_decls_become_units_in_source_order():
    _, order, defs = parse_module(MODULE)
    assert order == ["greeting", "Greeter", "Greeter.Greet", "punctuation", "count", "main"]
    assert set(order) == set(defs)


def test_a_defs_source_is_self_contained_and_keeps_its_doc_comment():
    _, _, defs = parse_module(MODULE)
    assert defs["greeting"].startswith("// greeting is the thing we say.")
    assert defs["greeting"].rstrip().endswith("}")
    assert "type Greeter" not in defs["greeting"]


def test_grouped_declarations_stay_one_unit():
    src = 'package main\n\nvar (\n\ta = 1\n\tb = 2\n)\n\nfunc f() int { return a + b }\n'
    _, order, defs = parse_module(src)
    assert order == ["a", "f"]           # the group is one unit, named for its first name
    assert "b = 2" in defs["a"]


def test_render_round_trips_through_split():
    preamble, order, defs = parse_module(MODULE)
    rendered = render_module(preamble, order, defs)
    p2, o2, d2 = parse_module(rendered)
    assert o2 == order
    assert {k: normalize_hash(v) for k, v in d2.items()} == \
           {k: normalize_hash(v) for k, v in defs.items()}


def test_render_output_is_gofmt_shaped():
    # gofmt wants exactly one blank line between top-level decls, and no jamming
    preamble, order, defs = parse_module(MODULE)
    rendered = render_module(preamble, order, defs)
    assert rendered.startswith("package main")
    assert rendered.endswith("}\n")
    assert "}\nfunc" not in rendered and "}\n\n\nfunc" not in rendered
    assert "}\n\nfunc" in rendered


def test_empty_and_preamble_only_modules_do_not_explode():
    preamble, order, defs = parse_module("package main\n")
    assert order == [] and defs == {}
    assert preamble.strip() == "package main"


# --- free names (the dependency edge set) ----------------------------------

def test_free_names_include_called_siblings_and_packages():
    _, _, defs = parse_module(MODULE)
    free = free_names(defs["main"])
    assert "greeting" in free
    assert "fmt" in free


def test_free_names_exclude_keywords_builtins_and_the_units_own_name():
    src = "func f(xs []int) int {\n\tn := len(xs)\n\tif n > 0 {\n\t\treturn n\n\t}\n\treturn 0\n}\n"
    free = free_names(src)
    for excluded in ("func", "if", "return", "int", "len", "f"):
        assert excluded not in free, f"{excluded!r} should not be a free name"


def test_free_names_exclude_selector_suffixes():
    # `fmt.Println` is a reference to `fmt`, not to a top-level `Println`
    free = free_names('func f() {\n\tfmt.Println("x")\n}\n')
    assert "fmt" in free and "Println" not in free


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
