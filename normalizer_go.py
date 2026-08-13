"""normalizer_go.py -- braid's Go frontend: layer 0 canonicalization + top-level decl split.

The Python frontend (`normalizer.py`) gets layers 0 and 2 because stdlib `ast` hands it a
real parse tree and Python's scope rules are small enough to re-implement honestly. Go has
no parser in the Python stdlib, so this frontend is deliberately shallower:

  layer 0  lex to a canonical token stream  -> comments, whitespace, line breaks, tabs-vs-
                                               spaces and redundant semicolons vanish
  layer 2  alpha-rename                     -> NOT DONE. Renaming an identifier without a
                                               scope analysis would collapse distinct
                                               entities, so per DESIGN.md s.3 ("be a
                                               coward") this frontend does not rename.

That still buys the thing that matters most in practice: a reformatted, re-commented,
gofmt'd-differently file is a *no-op* to braid. But two Go definitions that differ only in
local variable names hash differently, where their Python equivalents would not. Any
future Go layer-2 needs a real scope analysis, which needs a real parser.

Automatic semicolon insertion is the interesting part of the fold. Go's grammar wants
semicolons; the source usually omits them. So the canonical form *inserts* them (the same
rule the Go spec gives the scanner) and then drops the ones that carry no information --
duplicates, and any that sit immediately before a closing brace or paren. That makes
`a := 1; b := 2` and the same two statements on two lines the same program, which is what
content-addressing needs.

Splitting is brace-aware rather than parse-tree-accurate: a top-level `func`/`type`/`var`/
`const` (with any doc comment sitting directly above it) becomes a unit; `package` and
`import` are the file's preamble. A grouped declaration -- `var ( a = 1; b = 2 )` -- stays
one unit, named for its first name, because splitting a group would change the source.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from normalizer import Normalization

KEYWORDS = frozenset("""
break case chan const continue default defer else fallthrough for func go goto if import
interface map package range return select struct switch type var
""".split())

# Predeclared identifiers: types, constants, and builtins. Free by definition, but never a
# unit of a braid repo, so excluding them keeps the dependency edge set meaningful.
PREDECLARED = frozenset("""
bool byte complex64 complex128 error float32 float64 int int8 int16 int32 int64 rune string
uint uint8 uint16 uint32 uint64 uintptr any comparable
true false iota nil
append cap clear close complex copy delete imag len make max min new panic print println
real recover
""".split())

DECL_KEYWORDS = ("package", "import", "func", "type", "var", "const")
PREAMBLE_KEYWORDS = ("package", "import")

_OPERATORS = (
    ["<<=", ">>=", "&^=", "..."]
    + ["+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=", "&&", "||", "<-", "++", "--",
       "==", "!=", "<=", ">=", ":=", "<<", ">>", "&^"]
    + list("+-*/%&|^<>=!()[]{},;.:~")
)

# Tokens after which the Go scanner inserts a semicolon at end of line (spec: "Semicolons").
_ASI_OPS = frozenset(["++", "--", ")", "]", "}"])
_ASI_KEYWORDS = frozenset(["break", "continue", "fallthrough", "return"])
_LITERAL_KINDS = frozenset(["number", "string", "rune"])

_OPEN = {"(": ")", "[": "]", "{": "}"}
_CLOSE = frozenset([")", "]", "}"])


class GoSyntaxError(ValueError):
    """The lexer ran off the end of a string, rune or block comment."""


@dataclass
class Tok:
    kind: str          # ident | keyword | number | string | rune | op | comment
    text: str
    start: int
    end: int
    nl_before: bool    # at least one newline separates this token from the previous one


def _is_ident_start(c: str) -> bool:
    return c == "_" or c.isalpha()


def _is_ident_part(c: str) -> bool:
    return c == "_" or c.isalnum()


def tokenize(src: str) -> list:
    """Lex Go source into tokens, comments included (callers drop them)."""
    toks: list = []
    i, n = 0, len(src)
    nl = False
    while i < n:
        c = src[i]

        if c in " \t\r\n":
            if c == "\n":
                nl = True
            i += 1
            continue

        start = i

        # comments
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            toks.append(Tok("comment", src[i:j], start, j, nl))
            i, nl = j, False
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            j = src.find("*/", i + 2)
            if j < 0:
                raise GoSyntaxError("unterminated block comment")
            j += 2
            toks.append(Tok("comment", src[i:j], start, j, nl))
            i, nl = j, False
            continue

        # raw string literal
        if c == "`":
            j = src.find("`", i + 1)
            if j < 0:
                raise GoSyntaxError("unterminated raw string literal")
            j += 1
            toks.append(Tok("string", src[i:j], start, j, nl))
            i, nl = j, False
            continue

        # interpreted string / rune literal
        if c in '"\'':
            j = i + 1
            while j < n and src[j] != c:
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "\n":
                    raise GoSyntaxError("newline in string literal")
                j += 1
            if j >= n:
                raise GoSyntaxError("unterminated string literal")
            j += 1
            toks.append(Tok("string" if c == '"' else "rune", src[i:j], start, j, nl))
            i, nl = j, False
            continue

        # numeric literal
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            j = i
            while j < n and (src[j].isalnum() or src[j] in "._"):
                # an exponent sign is part of the literal: 1e+9, 0x1p-2
                if src[j] in "eEpP" and j + 1 < n and src[j + 1] in "+-" and not src[i:j + 1].lower().startswith("0x") ^ (src[j] in "pP"):
                    j += 2
                    continue
                j += 1
            toks.append(Tok("number", src[i:j], start, j, nl))
            i, nl = j, False
            continue

        # identifier or keyword
        if _is_ident_start(c):
            j = i + 1
            while j < n and _is_ident_part(src[j]):
                j += 1
            word = src[i:j]
            toks.append(Tok("keyword" if word in KEYWORDS else "ident", word, start, j, nl))
            i, nl = j, False
            continue

        # operator / punctuation
        for op in _OPERATORS:
            if src.startswith(op, i):
                toks.append(Tok("op", op, start, i + len(op), nl))
                i, nl = i + len(op), False
                break
        else:
            raise GoSyntaxError(f"unexpected character {c!r} at offset {i}")

    return toks


def _asi_applies(prev: str, prev_kind: str) -> bool:
    if prev_kind in _LITERAL_KINDS or prev_kind == "ident":
        return True
    if prev_kind == "keyword":
        return prev in _ASI_KEYWORDS
    return prev in _ASI_OPS


def canonical_tokens(src: str) -> list:
    """The canonical token stream: comments dropped, semicolons inserted then minimized."""
    out: list = []           # [(text, kind)]
    pending_nl = False
    for t in tokenize(src):
        nl = t.nl_before or pending_nl
        if t.kind == "comment":
            pending_nl = nl or t.text.startswith("//") or "\n" in t.text
            continue
        if nl and out and _asi_applies(*out[-1]):
            out.append((";", "op"))
        out.append((t.text, t.kind))
        pending_nl = False

    cleaned: list = []
    for text, kind in out:
        if text == ";":
            if not cleaned or cleaned[-1][0] in (";", "{", "("):
                continue                      # duplicate or empty statement
        elif text in ("}", ")"):
            while cleaned and cleaned[-1][0] == ";":
                cleaned.pop()                 # a semicolon may be omitted before ) or }
        cleaned.append((text, kind))
    while cleaned and cleaned[-1][0] == ";":
        cleaned.pop()
    return cleaned


def normalize(src: str) -> Normalization:
    canonical = " ".join(text for text, _ in canonical_tokens(src))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    # No alpha-renaming (see the module docstring), so there is no presentation metadata.
    return Normalization(hash=digest, canonical=canonical, names={})


def normalize_hash(src: str) -> str:
    return normalize(src).hash


# --- top-level declaration split -------------------------------------------

def _matching(toks: list, i: int) -> int:
    """Index of the bracket closing the one at `i`."""
    want = _OPEN[toks[i].text]
    depth = 0
    for k in range(i, len(toks)):
        t = toks[k]
        if t.kind != "op":
            continue
        if t.text in _OPEN:
            depth += 1
        elif t.text in _CLOSE:
            depth -= 1
            if depth == 0:
                if t.text != want:
                    raise GoSyntaxError(f"mismatched bracket at offset {t.start}")
                return k
    raise GoSyntaxError(f"unclosed {toks[i].text} at offset {toks[i].start}")


def _code_tokens(toks: list) -> list:
    return [t for t in toks if t.kind != "comment"]


def _decl_end(toks: list, i: int) -> int:
    """Index of the last token of the declaration whose keyword is at `i`."""
    kw = toks[i].text
    nxt = toks[i + 1] if i + 1 < len(toks) else None

    if kw != "func" and nxt is not None and nxt.kind == "op" and nxt.text == "(":
        return _matching(toks, i + 1)                      # grouped: var ( ... )

    if kw == "func":
        k = i + 1
        while k < len(toks):
            t = toks[k]
            if t.kind == "op" and t.text in ("(", "["):
                k = _matching(toks, k) + 1                 # receiver, params, type params
                continue
            if t.kind == "op" and t.text == "{":
                return _matching(toks, k)                  # the body
            if t.nl_before:
                return k - 1                               # bodiless declaration
            k += 1
        return len(toks) - 1

    depth = 0                                              # single-line var/const/type/...
    k = i + 1
    while k < len(toks):
        t = toks[k]
        if depth == 0 and t.nl_before:
            return k - 1
        if t.kind == "op":
            if t.text in _OPEN:
                depth += 1
            elif t.text in _CLOSE:
                depth -= 1
        k += 1
    return len(toks) - 1


def _decl_name(toks: list, i: int) -> str:
    """The unit name for the declaration whose keyword is at `i` (methods are Type.Name)."""
    kw = toks[i].text
    k = i + 1
    if kw == "func" and k < len(toks) and toks[k].kind == "op" and toks[k].text == "(":
        close = _matching(toks, k)
        recv = [t.text for t in toks[k + 1:close] if t.kind in ("ident", "keyword")]
        typ = recv[-1] if recv else "?"
        k = close + 1
        name = toks[k].text if k < len(toks) else "?"
        return f"{typ}.{name}"
    if k < len(toks) and toks[k].kind == "op" and toks[k].text == "(":
        close = _matching(toks, k)
        for t in toks[k + 1:close]:                        # first name in the group
            if t.kind == "ident":
                return t.text
        return "?"
    return toks[k].text if k < len(toks) else "?"


def _doc_start(toks: list, idx: int, text: str) -> int:
    """Index of the first token of the doc-comment block directly above token `idx`."""
    start = idx
    j = idx - 1
    while j >= 0 and toks[j].kind == "comment":
        gap = text[toks[j].end:toks[start].start]
        if gap.count("\n") != 1:
            break                                          # blank line, or same line
        before = text.rfind("\n", 0, toks[j].start)
        if text[before + 1:toks[j].start].strip():
            break                                          # trailing comment, not a doc
        start = j
        j -= 1
    return start


def parse_module(text: str):
    """Return (preamble, order, defs) for one Go file's top level."""
    toks = tokenize(text)
    code = _code_tokens(toks)
    if not code:
        return text.strip(), [], {}

    preamble_parts, order, defs = [], [], {}
    ci = 0
    while ci < len(code):
        t = code[ci]
        if not (t.kind == "keyword" and t.text in DECL_KEYWORDS):
            ci += 1
            continue
        end_ci = _decl_end(code, ci)
        ti = toks.index(t)
        seg_start = toks[_doc_start(toks, ti, text)].start
        seg = text[seg_start:code[end_ci].end].strip()
        if t.text in PREAMBLE_KEYWORDS:
            preamble_parts.append(seg)
        else:
            name = _decl_name(code, ci)
            base, k = name, 2
            while name in defs:                            # keep every decl addressable
                name = f"{base}#{k}"
                k += 1
            defs[name] = seg + "\n"
            order.append(name)
        ci = end_ci + 1

    return "\n\n".join(preamble_parts), order, defs


def render_module(preamble: str, order: list, defs: dict) -> str:
    parts = []
    if preamble.strip():
        parts.append(preamble.rstrip())
    seen = set()
    for name in order:
        if name in defs:
            parts.append(defs[name].rstrip())
            seen.add(name)
    for name in defs:
        if name not in seen:
            parts.append(defs[name].rstrip())
    return "\n\n".join(parts) + "\n"


def file_state(text: str) -> dict:
    preamble, order, defs = parse_module(text)
    return {"preamble": preamble, "order": order, "defs": defs}


# --- free (external) names -------------------------------------------------

def free_names(src: str) -> set:
    """The identifiers a declaration references, over-approximated.

    Without a scope analysis a local `count` is indistinguishable from a package-level
    `count`, so locals are included. The reconciler intersects this with the set of unit
    names and uses the result to decide *coupling*, and an extra edge only ever demotes an
    auto-merge from Tier 0 (disjoint) to Tier 1 (dependency-coupled) -- both are admitted.
    Over-approximating is therefore the conservative direction; under-approximating, which
    would let genuinely coupled changes look independent, is not.
    """
    toks = _code_tokens(tokenize(src))
    own: set = set()
    if toks and toks[0].kind == "keyword" and toks[0].text in DECL_KEYWORDS:
        name = _decl_name(toks, 0)
        own = {name, name.split(".")[-1], name.split(".")[0]}

    found: set = set()
    for k, t in enumerate(toks):
        if t.kind != "ident" or t.text in PREDECLARED or t.text in own:
            continue
        prev = toks[k - 1] if k else None
        if prev is not None and prev.kind == "op" and prev.text == ".":
            continue                                       # selector: x.Field, pkg.Name
        found.add(t.text)
    return found
