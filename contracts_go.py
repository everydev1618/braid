"""contracts_go.py -- run executable contracts against a composed Go codebase.

The Python gate (`contracts.py`) can `exec` a codebase into a namespace because Python is
already running. Go has to be built, so the gate here is: write the composed units out as a
scratch module, `go build` it, then run each contract as a test function. Same signature and
same return shape as `contracts.py` -- a non-empty list of `(contract_id, error)` means the
composition is red and the session must not be admitted.

A Go contract is the body of a `testing.T` test: the source gets pasted into

    func TestBraidContractN(t *testing.T) { <contract source> }

so `if greeting() != "Hello, World!" { t.Fatal("regressed") }` is the idiomatic form, and a
plain `panic(...)` works too.

Two things the Python gate gets for free and this one has to buy:
  - **`go` must be on PATH.** Without it there is no gate at all, and a gate that cannot run
    reports red (`<toolchain>`), never green -- an unverifiable merge is not an admissible one.
  - **Attribution.** One bad contract breaks the whole test binary, which yields no per-test
    output, so a build failure of the test binary falls back to compiling each contract on its
    own to find the culprit.

Like the Python gate this compiles and runs the code under test: fine for the prototype, not
a sandbox. Do not point it at untrusted input.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from normalizer_go import render_module, tokenize

Codebase = dict   # {name: source}
Contract = tuple  # (id, source)

SEP = "::"
PREAMBLE = "__preamble__"
DEFAULT_PATH = "main.go"
CONTRACT_FILE = "braid_contract_test.go"
BUILD_TIMEOUT = 180

_FAIL_RE = re.compile(r"^--- FAIL: TestBraidContract(\d+)", re.MULTILINE)


def go_available() -> bool:
    return shutil.which("go") is not None


def _run(args: list, cwd: str):
    env = dict(os.environ, GOPROXY="off", GOFLAGS="-mod=mod")
    return subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True,
                          timeout=BUILD_TIMEOUT)


def _go_directive() -> str:
    try:
        out = _run(["go", "env", "GOVERSION"], cwd=tempfile.gettempdir()).stdout.strip()
    except Exception:
        return "1.21"
    m = re.match(r"go(\d+)\.(\d+)", out)
    return f"{m.group(1)}.{m.group(2)}" if m else "1.21"


def _package_name(text: str) -> str | None:
    try:
        toks = [t for t in tokenize(text) if t.kind != "comment"]
    except Exception:
        return None
    for k, t in enumerate(toks):
        if t.kind == "keyword" and t.text == "package" and k + 1 < len(toks):
            return toks[k + 1].text
    return None


def materialize(codebase: Codebase) -> dict:
    """Compose the units back into `{relpath: file text}`, ready to be written out."""
    files: dict = {}
    for key, src in codebase.items():
        path, name = key.split(SEP, 1) if SEP in key else (DEFAULT_PATH, key)
        f = files.setdefault(path, {"preamble": "", "order": [], "defs": {}})
        if name == PREAMBLE:
            f["preamble"] = src.rstrip()
        else:
            f["defs"][name] = src
            f["order"].append(name)
    out = {}
    for path, f in files.items():
        preamble = f["preamble"] or "package main"
        out[path] = render_module(preamble, f["order"], f["defs"])
    return out


def _write_tree(root: str, files: dict) -> str:
    pkg = "main"
    for path, text in files.items():
        full = os.path.join(root, path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)
        pkg = _package_name(text) or pkg
    with open(os.path.join(root, "go.mod"), "w", encoding="utf-8") as fh:
        fh.write(f"module braidmain\n\ngo {_go_directive()}\n")
    return pkg


def _contract_file(pkg: str, contracts: list) -> str:
    parts = [f"package {pkg}", "", 'import "testing"', ""]
    for i, (_cid, csrc) in enumerate(contracts):
        parts.append(f"func TestBraidContract{i}(t *testing.T) {{\n{csrc}\n}}")
        parts.append("")
    return "\n".join(parts)


def _attribute_one_by_one(root: str, pkg: str, contracts: list) -> list:
    """The test binary would not build; find which contract(s) are to blame."""
    failures = []
    path = os.path.join(root, CONTRACT_FILE)
    for i, contract in enumerate(contracts):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(_contract_file(pkg, [contract]))
        proc = _run(["go", "test", "-count=1", "-run", "TestBraidContract", "./..."], root)
        if proc.returncode != 0:
            failures.append((contract[0], _clean(proc.stdout + proc.stderr)))
    os.remove(path)
    return failures


def _clean(output: str, limit: int = 600) -> str:
    text = "\n".join(line for line in output.splitlines()
                     if line.strip() and not line.startswith(("ok ", "---", "===", "FAIL\t",
                                                              "PASS", "FAIL")))
    return (text[:limit] + " ...") if len(text) > limit else text or "failed"


def run_contracts(codebase: Codebase, contracts) -> list:
    """Return `[(contract_id, error)]` for every failing contract; `[]` if all green."""
    contracts = [tuple(c) for c in contracts]
    if not go_available():
        return [("<toolchain>", "go not found on PATH: cannot verify a Go composition")]

    with tempfile.TemporaryDirectory(prefix="braid-go-") as root:
        try:
            pkg = _write_tree(root, materialize(codebase))
        except Exception as e:                              # unlexable source, bad units
            return [("<materialize>", f"{type(e).__name__}: {e}")]

        try:
            build = _run(["go", "build", "./..."], root)
        except subprocess.TimeoutExpired:
            return [("<materialize>", "go build timed out")]
        if build.returncode != 0:
            return [("<materialize>", _clean(build.stdout + build.stderr))]

        if not contracts:
            return []

        with open(os.path.join(root, CONTRACT_FILE), "w", encoding="utf-8") as fh:
            fh.write(_contract_file(pkg, contracts))
        try:
            proc = _run(["go", "test", "-count=1", "-v", "-run", "TestBraidContract", "./..."], root)
        except subprocess.TimeoutExpired:
            return [(cid, "go test timed out") for cid, _ in contracts]
        if proc.returncode == 0:
            return []

        failed = [int(n) for n in _FAIL_RE.findall(proc.stdout)]
        if not failed:                                      # the test binary did not build
            return _attribute_one_by_one(root, pkg, contracts)

        details = _split_failures(proc.stdout)
        return [(contracts[i][0], details.get(i, "contract failed")) for i in sorted(failed)]


def _split_failures(output: str) -> dict:
    """Per-test failure text, keyed by contract index.

    `go test -v` prints a test's output *between* its `=== RUN` and its `--- FAIL` lines, so
    the body has to be collected from the RUN marker forward. Anchoring on `--- FAIL` and
    reading on captures the *next* test's output instead, and nothing at all for the last one.
    """
    details, current, buf = {}, None, []
    for line in output.splitlines():
        run = re.match(r"^\s*=== RUN\s+TestBraidContract(\d+)", line)
        if run:
            current, buf = int(run.group(1)), []
            continue
        done = re.match(r"^\s*--- (PASS|FAIL): TestBraidContract(\d+)", line)
        if done:
            if done.group(1) == "FAIL":
                details[int(done.group(2))] = _clean("\n".join(buf))
            current, buf = None, []
            continue
        if current is not None:
            buf.append(line)
    return details
