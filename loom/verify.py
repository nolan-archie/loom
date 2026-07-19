"""stage 3 - verify, post-apply structural sanity check (design doc §5).

confirms two things about a tree after wire/restage has written to it:

1. macro presence
   for each file the patch touches, work out which CONFIG_KSU_SUSFS*
   macros the patch actually introduces *in that file* (by scanning the
   hunk's own added/context lines, not the whole-patch macro list - a
   file only needs to carry the macros its own hunks reference). Confirm
   every one of those macros is still present in the file on disk
   afterward. This catches a class of bug hunk-level "applied" status
   can't: a tier can report a hunk applied while having spliced it in
   relative to a subtly-wrong location, and file-level review is what
   catches that class of error, not the per-hunk report.

2. syntactic sanity
   for .c/.h files, confirm the result still parses as one well-formed
   translation unit. Prefers tree-sitter's C grammar and its own error
   recovery (`tree.root_node.has_error`) when tree-sitter-c is
   installed (same optional dependency Tier 2 uses); falls back to a
   comment/string-aware brace/paren/bracket balance scan when it isn't,
   so verify never hard-requires Tier 2's optional extra.

Optional, off by default (--compile): runs `cc -fsyntax-only` per file.
This is best-effort and advisory only, never affects verify's pass/fail
status - a kernel .c file essentially never compiles standalone outside
the kbuild system (missing autoconf.h, generated headers, arch-specific
includes), so failure here is expected and uninformative on its own.
It's still useful as an opt-in second opinion when a toolchain happens
to be available and the tree is close to buildable.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .cascade import parse_hunks
from .patchutils import MACRO_RE

KBUILD_FILENAMES = {"Makefile", "Kbuild"}


@dataclass
class FileVerifyResult:
    path: str
    status: str  # ok / missing-macros / syntax-error / not-found
    expected_macros: list[str] = field(default_factory=list)
    missing_macros: list[str] = field(default_factory=list)
    syntax_ok: bool | None = None  # None = not applicable (not a .c/.h file)
    syntax_detail: str = ""
    syntax_method: str = ""  # tree-sitter / brace-balance / skipped
    compile_checked: bool = False
    compile_ok: bool | None = None
    compile_detail: str = ""


@dataclass
class VerifyReport:
    results: list[FileVerifyResult] = field(default_factory=list)

    @property
    def failed(self) -> list[FileVerifyResult]:
        return [r for r in self.results if r.status not in ("ok",)]


def _expected_macros_per_file(patch_text: str) -> dict[str, set[str]]:
    """which CONFIG_KSU_SUSFS* macros does the patch introduce, per file -
    scanning each hunk's own new-image text, not the whole-patch macro
    list, since a given file only needs to carry the macros its own
    hunks actually reference."""
    per_file: dict[str, set[str]] = {}
    for fd in parse_hunks(patch_text):
        macros: set[str] = set()
        for hunk in fd.hunks:
            macros |= set(MACRO_RE.findall("\n".join(hunk.new_image)))
        if macros:
            per_file.setdefault(fd.new_path, set()).update(macros)
    return per_file


def _syntax_check_tree_sitter(text: str) -> tuple[bool, str] | None:
    """returns (ok, detail) using tree-sitter's own C grammar error
    recovery, or None if tree-sitter/tree-sitter-c aren't installed."""
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_c
    except ImportError:
        return None

    parser = Parser(Language(tree_sitter_c.language()))
    tree = parser.parse(text.encode())
    if not tree.root_node.has_error:
        return True, "parsed with no ERROR nodes (tree-sitter C grammar)"

    # find the first ERROR node so the report points somewhere useful
    def find_error(node):
        if node.type == "ERROR" or node.is_missing:
            return node
        for child in node.children:
            found = find_error(child)
            if found is not None:
                return found
        return None

    bad = find_error(tree.root_node)
    line = (bad.start_point.row + 1) if bad is not None else "?"
    return False, f"parse error near line {line} (tree-sitter C grammar)"


_STRING_OR_COMMENT_RE = re.compile(
    r"""//[^\n]*                     # line comment
      | /\*.*?\*/                    # block comment
      | "(?:\\.|[^"\\])*"            # string literal
      | '(?:\\.|[^'\\])*'            # char literal
    """,
    re.VERBOSE | re.DOTALL,
)


def _syntax_check_brace_balance(text: str) -> tuple[bool, str]:
    """fallback when tree-sitter isn't installed: strip comments/string
    literals, then confirm (), {}, [] all balance and nest correctly.
    weaker than a real parse (won't catch e.g. a misplaced semicolon)
    but catches the failure mode that actually matters here - a bad
    splice leaving an unbalanced brace/paren behind."""
    stripped = _STRING_OR_COMMENT_RE.sub(" ", text)
    pairs = {")": "(", "}": "{", "]": "["}
    stack: list[tuple[str, int]] = []
    line = 1
    for ch in stripped:
        if ch == "\n":
            line += 1
            continue
        if ch in "({[":
            stack.append((ch, line))
        elif ch in ")}]":
            if not stack or stack[-1][0] != pairs[ch]:
                return False, f"unbalanced '{ch}' at line {line} (brace-balance scan)"
            stack.pop()
    if stack:
        ch, at_line = stack[-1]
        return False, f"unclosed '{ch}' opened at line {at_line} (brace-balance scan)"
    return True, "braces/parens/brackets balanced (brace-balance scan)"


def _compile_check(fpath: Path) -> tuple[bool, str]:
    cc = shutil.which("cc") or shutil.which("gcc")
    if not cc:
        return False, "no C compiler on PATH"
    proc = subprocess.run(
        [cc, "-fsyntax-only", "-w", str(fpath)],
        capture_output=True, text=True, cwd=str(fpath.parent),
    )
    if proc.returncode == 0:
        return True, "compiled clean"
    # kernel files basically always fail this outside kbuild (missing
    # generated headers, arch config, etc.) - report the reason but
    # don't editorialize about whether it's loom's fault.
    first_error = next(
        (ln for ln in proc.stderr.splitlines() if "error:" in ln), proc.stderr.splitlines()[:1] or [""]
    )[0] if proc.stderr else "(no stderr)"
    return False, f"cc -fsyntax-only failed: {first_error.strip()}"


def verify_tree(
    tree_path: str | Path, patch_path: str | Path, *, compile_check: bool = False,
) -> VerifyReport:
    tree = Path(tree_path)
    patch_text = Path(patch_path).read_text(errors="replace")
    per_file_macros = _expected_macros_per_file(patch_text)
    report = VerifyReport()

    for rel, expected in sorted(per_file_macros.items()):
        fpath = tree / rel
        if not fpath.is_file():
            report.results.append(FileVerifyResult(
                path=rel, status="not-found",
                expected_macros=sorted(expected),
            ))
            continue

        text = fpath.read_text(errors="replace")
        present = set(MACRO_RE.findall(text))
        missing = sorted(expected - present)

        is_c_source = fpath.suffix in {".c", ".h"}
        syntax_ok: bool | None = None
        syntax_detail = ""
        syntax_method = "skipped (not a .c/.h file)"
        if is_c_source:
            ts_result = _syntax_check_tree_sitter(text)
            if ts_result is not None:
                syntax_ok, syntax_detail = ts_result
                syntax_method = "tree-sitter"
            else:
                syntax_ok, syntax_detail = _syntax_check_brace_balance(text)
                syntax_method = "brace-balance"

        compile_ok: bool | None = None
        compile_detail = ""
        if compile_check and is_c_source:
            compile_ok, compile_detail = _compile_check(fpath)

        if missing:
            status = "missing-macros"
        elif syntax_ok is False:
            status = "syntax-error"
        else:
            status = "ok"

        report.results.append(FileVerifyResult(
            path=rel, status=status,
            expected_macros=sorted(expected), missing_macros=missing,
            syntax_ok=syntax_ok, syntax_detail=syntax_detail, syntax_method=syntax_method,
            compile_checked=compile_check and is_c_source,
            compile_ok=compile_ok, compile_detail=compile_detail,
        ))

    return report
