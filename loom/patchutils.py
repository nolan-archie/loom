"""stuff for pulling info out of a raw diff file. nothing here is really
susfs-specific except the macro prefix, could reuse for any ifdef-guarded
patch if we ever need to."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

MACRO_RE = re.compile(r"CONFIG_KSU_SUSFS[A-Z0-9_]*")

# reading the +++ side instead of "diff --git a/... b/..." because git quotes
# paths with spaces inconsistently across versions and I got bit by that once
PLUS_HEADER_RE = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+?)(?:\t.*)?$", re.MULTILINE)

# obj-$(CONFIG_KSU_SUSFS) += susfs.o  -- kbuild doesn't know what #ifdef is
KBUILD_COND_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<lhs>[\w.-]+-)\$\((?P<macro>CONFIG_KSU_SUSFS[A-Z0-9_]*)\)"
    r"(?P<rest>\s*[:+]?=.*)$"
)


@dataclass
class PatchInfo:
    path: Path
    macros: list[str] = field(default_factory=list)
    touched_files: list[str] = field(default_factory=list)


def load_patch(patch_path: str | Path) -> PatchInfo:
    patch_path = Path(patch_path)
    text = patch_path.read_text(errors="replace")

    macros = sorted(set(MACRO_RE.findall(text)))

    touched, seen = [], set()
    for m in PLUS_HEADER_RE.finditer(text):
        p = m.group("path").strip()
        if p == "/dev/null" or p in seen:
            continue
        seen.add(p)
        touched.append(p)

    return PatchInfo(path=patch_path, macros=macros, touched_files=touched)


def find_kbuild_conditional_lines(file_text: str, macros: set[str]) -> list[tuple[int, str]]:
    """returns (lineno, line) for any kbuild line gated on one of our macros"""
    hits = []
    for i, line in enumerate(file_text.splitlines(), start=1):
        m = KBUILD_COND_RE.match(line)
        if m and m.group("macro") in macros:
            hits.append((i, line))
    return hits


def find_surviving_tokens(file_text: str) -> list[tuple[int, str]]:
    """after stripping, anything matching this is either an unguarded
    insertion or someone hand-fixed a .rej and forgot to put the ifdef back.
    either way needs a human to look at it, we don't try to guess"""
    token_re = re.compile(r"\bsusfs_\w*|\bKSU_SUSFS\w*", re.IGNORECASE)
    hits = []
    for i, line in enumerate(file_text.splitlines(), start=1):
        if token_re.search(line):
            hits.append((i, line.strip()))
    return hits
