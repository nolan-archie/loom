"""stage 2 - cascade apply, tiers 0-1 only. tiers 2-4 (anchor relocation,
semantic patch, human handoff) are still design-only, see design doc §5.

works per-hunk, not per-file - each hunk in the patch is tried against the
tree independently, escalating tolerance for drift, and tagged with
whichever tier actually resolved it:

  tier 0 - exact
      the hunk's recorded context/old-lines match the tree verbatim at the
      offset the patch says they should be at (adjusted for any earlier
      hunks in the same file we've already placed this run). this is what
      plain `patch -p1` / `git apply` does.

  tier 1 - 3-way merge
      exact match at that offset failed, which almost always means
      something ELSE in the file shifted line numbers (vendor added or
      removed lines above this hunk) - not that this exact block changed.
      locate the closest-matching window for the hunk's old-image
      elsewhere in the current file (difflib), then run a real three-way
      merge with `git merge-file`: patch's own recorded before-image is
      the merge base, patch's after-image is "theirs", the located window
      in the tree is "ours". this succeeds even when unrelated lines
      drifted around the hook site, and only conflicts when the located
      block and the patch's own edit genuinely collide - which is exactly
      the "minor context drift vs. real conflict" distinction tier 1 is
      supposed to draw (design doc §5, Stage 2).

worth noting `git merge-file` doesn't need the tree to be a git repo or
the original blob to exist in any object database - the base/theirs text
comes straight out of the patch file itself, which already contains it.
that matters here because vendor kernel trees dumped onto disk from a
factory image often aren't git repos at all.

anything neither tier resolves is left untouched on disk and reported as
unresolved, needing tier 2 (anchor relocation) or tier 3 (semantic patch)
or tier 4 (human handoff) - none of which exist yet. a hunk is never
marked applied without knowing which tier did it; that bookkeeping is the
entire point of a cascade instead of one pass/fail patch call.
"""
from __future__ import annotations

import difflib
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

HUNK_HEADER_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$")
DIFF_GIT_RE = re.compile(r"^diff --git a/(.+?) b/(.+?)$")

# how far past the recorded offset we'll search for a relocated context
# block before giving up on tier 1. wide enough to survive a few dozen
# lines of unrelated vendor churn above the hook site; wider than that and
# a "closest match" starts being more likely wrong than useful (tier 2's
# job, not tier 1's).
SEARCH_WINDOW = 400


@dataclass
class Hunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    section: str  # the bit after the second @@, often a function name
    lines: list[tuple[str, str]] = field(default_factory=list)  # (' '/'-'/'+', text)

    @property
    def old_image(self) -> list[str]:
        return [t for m, t in self.lines if m in (" ", "-")]

    @property
    def new_image(self) -> list[str]:
        return [t for m, t in self.lines if m in (" ", "+")]


@dataclass
class FileDiff:
    old_path: str
    new_path: str
    hunks: list[Hunk] = field(default_factory=list)


@dataclass
class HunkResult:
    file: str
    hunk_index: int  # 0-based, in patch order, for this file
    section: str
    tier: int | None  # 0, 1, or None if unresolved
    status: str  # applied / conflict / unresolved / file-not-found
    detail: str = ""
    conflict_text: str | None = None  # only set on tier-1 conflict, for tier-4 handoff later


@dataclass
class CascadeReport:
    results: list[HunkResult] = field(default_factory=list)
    # new file content per touched path, ONLY for files where every hunk in
    # them resolved (see module docstring re: never partially writing a file
    # when some of its hunks still need a tier that doesn't exist yet)
    resolved_file_text: dict[str, str] = field(default_factory=dict)

    @property
    def unresolved(self) -> list[HunkResult]:
        return [r for r in self.results if r.tier is None]

    @property
    def by_tier_count(self) -> dict[str, int]:
        counts = {"tier-0": 0, "tier-1": 0, "unresolved": 0}
        for r in self.results:
            counts["tier-0" if r.tier == 0 else "tier-1" if r.tier == 1 else "unresolved"] += 1
        return counts


def parse_hunks(patch_text: str) -> list[FileDiff]:
    """splits a unified diff into per-file, per-hunk structure. deliberately
    doesn't reuse patchutils' PLUS_HEADER_RE-only scan - that one only needed
    the file list, this needs the actual hunk bodies."""
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: Hunk | None = None

    for line in patch_text.splitlines():
        m = DIFF_GIT_RE.match(line)
        if m:
            if current is not None:
                files.append(current)
            current = FileDiff(old_path=m.group(1), new_path=m.group(2))
            current_hunk = None
            continue

        if current is None:
            continue  # preamble before the first diff --git

        if line.startswith("--- ") or line.startswith("+++ "):
            continue

        hm = HUNK_HEADER_RE.match(line)
        if hm:
            old_start, old_count, new_start, new_count, section = hm.groups()
            current_hunk = Hunk(
                old_start=int(old_start),
                old_count=int(old_count) if old_count else 1,
                new_start=int(new_start),
                new_count=int(new_count) if new_count else 1,
                section=section.strip(),
            )
            current.hunks.append(current_hunk)
            continue

        if current_hunk is None:
            continue  # e.g. "index abc123..def456 100644" lines

        if line.startswith(("+", "-", " ")):
            current_hunk.lines.append((line[0], line[1:]))
        elif line == r"\ No newline at end of file":
            continue

    if current is not None:
        files.append(current)
    return files


def _try_tier0(current_lines: list[str], hunk: Hunk, offset: int) -> list[str] | None:
    """exact match at the patch's recorded offset, shifted by whatever
    earlier hunks in this file already added/removed this run. returns the
    spliced result or None if the context didn't match."""
    start = hunk.old_start - 1 + offset
    old_image = hunk.old_image
    end = start + len(old_image)
    if start < 0 or end > len(current_lines):
        return None
    if current_lines[start:end] != old_image:
        return None
    return current_lines[:start] + hunk.new_image + current_lines[end:]


def _locate_best_window(current_lines: list[str], old_image: list[str], approx_start: int) -> tuple[int, float] | None:
    """slides a window the length of old_image across a region of the
    current file around approx_start, scoring each position by similarity
    to old_image, returns (best_start, ratio) or None if nothing scored
    above threshold. this is deliberately a local search (± SEARCH_WINDOW)
    not a whole-file search - a whole-file search risks matching a
    superficially-similar but wrong location (e.g. the same idiom repeated
    in another function), which is exactly the mistake tier 2's AST-aware
    relocation exists to avoid making. tier 1 stays conservative on purpose."""
    if not old_image:
        return None
    n = len(old_image)
    lo = max(0, approx_start - SEARCH_WINDOW)
    hi = min(len(current_lines), approx_start + SEARCH_WINDOW + n)

    best: tuple[int, float] | None = None
    sm = difflib.SequenceMatcher(autojunk=False)
    sm.set_seq2(old_image)
    for start in range(lo, max(lo, hi - n) + 1):
        window = current_lines[start:start + n]
        sm.set_seq1(window)
        ratio = sm.quick_ratio()
        if best is None or ratio > best[1]:
            best = (start, ratio)
    if best is None:
        return None
    # refine the winning candidate's ratio properly (quick_ratio is an
    # upper bound, cheap to compute for every position; real ratio only
    # needs computing once, for the position that already won on the cheap one)
    start, _ = best
    sm.set_seq1(current_lines[start:start + n])
    real_ratio = sm.ratio()
    return (start, real_ratio)


# below this similarity, we don't trust the located window enough to hand
# it to a 3-way merge - too likely to be the wrong spot entirely rather
# than the right spot with drift. this is a tier-1/tier-4 boundary call,
# not a tier-1/tier-2 one: tier 2 doesn't exist yet, so today "below
# threshold" just falls through to unresolved either way.
MATCH_THRESHOLD = 0.55


def _try_tier1(current_lines: list[str], hunk: Hunk, offset: int) -> tuple[list[str] | None, str | None]:
    """returns (result, conflict_text). result is None if unresolved
    (either no plausible location found, or merge-file produced conflict
    markers); conflict_text is only set in the conflict case, for a future
    tier-4 handoff view."""
    old_image = hunk.old_image
    approx_start = hunk.old_start - 1 + offset
    located = _locate_best_window(current_lines, old_image, approx_start)
    if located is None or located[1] < MATCH_THRESHOLD:
        return None, None
    start, _ratio = located
    n = len(old_image)
    ours_block = current_lines[start:start + n]

    if not shutil.which("git"):
        return None, None

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ours_f = tdp / "ours"
        base_f = tdp / "base"
        theirs_f = tdp / "theirs"
        ours_f.write_text("\n".join(ours_block) + ("\n" if ours_block else ""))
        base_f.write_text("\n".join(old_image) + ("\n" if old_image else ""))
        theirs_f.write_text("\n".join(hunk.new_image) + ("\n" if hunk.new_image else ""))

        proc = subprocess.run(
            ["git", "merge-file", "-p", str(ours_f), str(base_f), str(theirs_f)],
            capture_output=True, text=True,
        )
        # exit codes: 0 clean, >0 = that many conflicts, <0 = real error
        if proc.returncode < 0:
            return None, None
        merged_lines = proc.stdout.split("\n")
        if merged_lines and merged_lines[-1] == "":
            merged_lines = merged_lines[:-1]

        if proc.returncode > 0:
            return None, "\n".join(merged_lines)

        return current_lines[:start] + merged_lines + current_lines[start + n:], None


def cascade_apply(tree_path: str | Path, patch_path: str | Path) -> CascadeReport:
    tree = Path(tree_path)
    patch_text = Path(patch_path).read_text(errors="replace")
    file_diffs = parse_hunks(patch_text)
    report = CascadeReport()

    for fd in file_diffs:
        rel = fd.new_path
        fpath = tree / rel
        if not fpath.is_file():
            for i, h in enumerate(fd.hunks):
                report.results.append(HunkResult(
                    file=rel, hunk_index=i, section=h.section,
                    tier=None, status="file-not-found",
                    detail="patch touches this file but it's not in the tree",
                ))
            continue

        current_lines = fpath.read_text(errors="replace").split("\n")
        trailing_newline = current_lines and current_lines[-1] == ""
        if trailing_newline:
            current_lines = current_lines[:-1]

        offset = 0
        file_had_unresolved = False
        for i, hunk in enumerate(fd.hunks):
            spliced = _try_tier0(current_lines, hunk, offset)
            if spliced is not None:
                current_lines = spliced
                offset += len(hunk.new_image) - len(hunk.old_image)
                report.results.append(HunkResult(
                    file=rel, hunk_index=i, section=hunk.section,
                    tier=0, status="applied",
                ))
                continue

            spliced, conflict_text = _try_tier1(current_lines, hunk, offset)
            if spliced is not None:
                offset += len(spliced) - len(current_lines)
                current_lines = spliced
                report.results.append(HunkResult(
                    file=rel, hunk_index=i, section=hunk.section,
                    tier=1, status="applied",
                ))
                continue

            file_had_unresolved = True
            status = "conflict" if conflict_text else "unresolved"
            detail = (
                "3-way merge produced conflict markers - genuine overlap, needs tier 4 (human handoff, not built)"
                if conflict_text else
                "no matching context found within search window - needs tier 2 (anchor relocation, not built) "
                "or tier 3 (semantic patch, not built)"
            )
            report.results.append(HunkResult(
                file=rel, hunk_index=i, section=hunk.section,
                tier=None, status=status, detail=detail, conflict_text=conflict_text,
            ))

        if not file_had_unresolved:
            text = "\n".join(current_lines)
            if trailing_newline:
                text += "\n"
            report.resolved_file_text[rel] = text

    return report


def apply_cascade(report: CascadeReport, tree_path: str | Path) -> list[str]:
    """writes back only files where every hunk resolved (tier 0 or 1) - a
    file with any unresolved hunk is left untouched on disk, same
    conservative rule strip.py uses for coverage-flagged files. partial
    writes would mean guessing at file state tier 2-4 are supposed to
    handle, silently."""
    tree = Path(tree_path)
    written = []
    for rel, text in report.resolved_file_text.items():
        (tree / rel).write_text(text)
        written.append(rel)
    return written
