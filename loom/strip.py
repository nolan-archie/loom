"""stage 1 - strip, restage mode only.

takes an already-hooked tree and reconstructs what it looked like before any
susfs hooks were applied, using the macro list from the NEW patch you're
about to apply. doesn't need the old patch file at all, which is good
because half the time nobody has it anymore after a maintainer hand-fixed
some rejects.

two strip mechanisms because susfs uses two different conditional-inclusion
styles and nothing understands both:
  - .c/.h files -> #ifdef blocks -> unifdef
  - Makefile/Kbuild -> obj-$(CONFIG_X) += y.o one-liners -> just regex it

after stripping we grep the result for leftover susfs tokens. if anything
survives we flag it and refuse to write that file back unless forced -
see find_surviving_tokens in patchutils for the reasoning.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .patchutils import KBUILD_COND_RE, PatchInfo, find_surviving_tokens, load_patch

KBUILD_FILENAMES = {"Makefile", "Kbuild"}


@dataclass
class FileStripResult:
    path: str
    kind: str  # c-preprocessor / kbuild / missing / skipped-no-macro-match
    status: str  # stripped / unchanged / coverage-flag / not-found / error
    detail: str = ""
    surviving_tokens: list[tuple[int, str]] = field(default_factory=list)
    new_text: str | None = None


@dataclass
class StripReport:
    patch: PatchInfo
    results: list[FileStripResult] = field(default_factory=list)

    @property
    def needs_review(self) -> list[FileStripResult]:
        return [r for r in self.results if r.status in ("coverage-flag", "error")]

    @property
    def clean_count(self) -> int:
        return len([r for r in self.results if r.status in ("stripped", "unchanged")])


def _strip_c_file(text: str, macros: list[str]) -> tuple[str, str]:
    # all macros in ONE unifdef call, not one call per macro - otherwise it
    # can't tell what to do with nested susfs-inside-susfs ifdefs
    args = ["unifdef"]
    for m in macros:
        args += ["-U", m]
    proc = subprocess.run(args, input=text, capture_output=True, text=True)
    if proc.returncode == 2:
        raise RuntimeError(f"unifdef error: {proc.stderr.strip()}")
    # unifdef exit codes: 0 = nothing changed, 1 = changed, 2 = error
    status = "unchanged" if proc.returncode == 0 else "stripped"
    return proc.stdout, status


def _strip_kbuild_file(text: str, macros: set[str]) -> tuple[str, str]:
    # susfs only ever ADDS kbuild lines, never wraps an existing one, so
    # deleting the matched lines is the correct inverse
    out_lines, changed = [], False
    for line in text.splitlines(keepends=True):
        m = KBUILD_COND_RE.match(line)
        if m and m.group("macro") in macros:
            changed = True
            continue
        out_lines.append(line)
    return "".join(out_lines), ("stripped" if changed else "unchanged")


def strip_tree(tree_path: str | Path, patch_path: str | Path) -> StripReport:
    tree = Path(tree_path)
    patch = load_patch(patch_path)
    macro_set = set(patch.macros)
    results: list[FileStripResult] = []

    if not patch.macros:
        raise ValueError(
            f"no CONFIG_KSU_SUSFS* macros found in {patch_path} - wrong file, "
            "or susfs changed its macro naming (see README 'when this breaks')"
        )

    for rel in patch.touched_files:
        fpath = tree / rel
        if not fpath.is_file():
            results.append(FileStripResult(
                path=rel, kind="missing", status="not-found",
                detail="patch touches this file but it's not in the tree",
            ))
            continue

        try:
            text = fpath.read_text(errors="replace")
        except OSError as e:
            results.append(FileStripResult(path=rel, kind="missing", status="error", detail=str(e)))
            continue

        is_kbuild = fpath.name in KBUILD_FILENAMES or fpath.suffix == ".mk"
        try:
            if is_kbuild:
                new_text, status = _strip_kbuild_file(text, macro_set)
                kind = "kbuild"
            else:
                new_text, status = _strip_c_file(text, patch.macros)
                kind = "c-preprocessor"
        except RuntimeError as e:
            results.append(FileStripResult(path=rel, kind="c-preprocessor", status="error", detail=str(e)))
            continue

        surviving = find_surviving_tokens(new_text)
        if surviving:
            results.append(FileStripResult(
                path=rel, kind=kind, status="coverage-flag",
                detail=f"{len(surviving)} susfs token(s) survived stripping",
                surviving_tokens=surviving, new_text=new_text,
            ))
        else:
            results.append(FileStripResult(path=rel, kind=kind, status=status, new_text=new_text))

    return StripReport(patch=patch, results=results)


def apply_strip(report: StripReport, tree_path: str | Path, *, skip_flagged: bool = True) -> list[str]:
    """writes stripped content back to disk. flagged files are skipped by
    default - writing a half-stripped file that still has live susfs tokens
    in it is exactly what the coverage check is trying to prevent"""
    tree = Path(tree_path)
    written = []
    for r in report.results:
        if r.new_text is None:
            continue
        if r.status == "coverage-flag" and skip_flagged:
            continue
        if r.status not in ("stripped", "coverage-flag"):
            continue
        (tree / r.path).write_text(r.new_text)
        written.append(r.path)
    return written
