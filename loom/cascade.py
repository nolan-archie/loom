"""stage 2 - cascade apply, tiers 0-2. tiers 3-4 (semantic patch and
human handoff) are still design-only, see design doc §5.

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

  tier 2 - AST anchor relocation
      when tier 1's deliberately bounded local search cannot find the
      context, use tree-sitter's C grammar to find the function named by
      the hunk header.  The hunk is still matched and three-way merged
      inside that one function; an AST anchor expands the search location,
      it does not make a blind textual edit.  Ambiguous/missing anchors and
      non-C files fall through safely.

worth noting `git merge-file` doesn't need the tree to be a git repo or
the original blob to exist in any object database - the base/theirs text
comes straight out of the patch file itself, which already contains it.
that matters here because vendor kernel trees dumped onto disk from a
factory image often aren't git repos at all.

anything no tier resolves is left untouched on disk and reported as
unresolved, needing tier 3 (semantic patch) or tier 4 (human handoff) -
neither exists yet. a hunk is never
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
    tier: int | None  # 0, 1, 2, or None if unresolved
    status: str  # applied / conflict / unresolved / file-not-found
    detail: str = ""
    conflict_text: str | None = None  # only set on tier-1 conflict, for tier-4 handoff later
    handoff_text: str | None = None  # closest target region for Tier 4 review
    handoff_start: int | None = None  # 1-based line number in the target file
    patch_old_text: str | None = None


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
        counts = {"tier-0": 0, "tier-1": 0, "tier-2": 0, "unresolved": 0}
        for r in self.results:
            counts[
                "tier-0" if r.tier == 0 else
                "tier-1" if r.tier == 1 else
                "tier-2" if r.tier == 2 else
                "unresolved"
            ] += 1
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


def _locate_best_window(
    current_lines: list[str], old_image: list[str], approx_start: int,
    search_window: int | None = SEARCH_WINDOW,
) -> tuple[int, float] | None:
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
    if search_window is None:
        lo, hi = 0, len(current_lines)
    else:
        lo = max(0, approx_start - search_window)
        hi = min(len(current_lines), approx_start + search_window + n)

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
# than the right spot with drift. Tier 2 may still retry inside a uniquely
# identified AST function anchor; otherwise it falls through to unresolved.
MATCH_THRESHOLD = 0.55


def _collapse_blank_runs(lines: list[str]) -> list[str]:
    """Collapse runs of 2+ consecutive blank lines down to a single blank
    line.

    Found via a real-tree test case: a vendor tree had reformatted
    ``kernel/reboot.c`` to have two blank lines where the patch's own
    recorded context has one. `_locate_best_window`'s similarity scoring
    is lenient enough to still find the right spot despite that, but
    `git merge-file`'s byte-exact base/ours comparison then treats the
    extra blank line as a genuine "ours" edit competing with the patch's
    own insertion at the same seam - producing a spurious conflict for a
    hunk that has no real logical collision at all.

    Blank-line *count* never carries semantic meaning in C, so
    normalizing it before handing base/ours to `git merge-file` is safe:
    every non-blank line stays byte-exact, only cosmetic run-length is
    collapsed. Applied identically to the base (patch's old-image) and
    ours (tree) sides so a whitespace-only difference between them
    produces no diff at all, leaving only the patch's real content change
    for merge-file to apply.
    """
    out: list[str] = []
    prev_blank = False
    for line in lines:
        blank = line.strip() == ""
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return out


def _merge_file(ours: list[str], base: list[str], theirs: list[str]) -> tuple[list[str] | None, str | None]:
    """runs `git merge-file -p` over three line-lists, normalizing blank-run
    noise between base and ours first (see `_collapse_blank_runs`).
    returns (merged_lines, None) on a clean merge, or (None, conflict_text)
    on conflict; (None, None) on a hard tool error (missing git, negative
    return code)."""
    if not shutil.which("git"):
        return None, None

    base = _collapse_blank_runs(base)
    ours = _collapse_blank_runs(ours)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ours_f, base_f, theirs_f = tdp / "ours", tdp / "base", tdp / "theirs"
        ours_f.write_text("\n".join(ours) + ("\n" if ours else ""))
        base_f.write_text("\n".join(base) + ("\n" if base else ""))
        theirs_f.write_text("\n".join(theirs) + ("\n" if theirs else ""))
        proc = subprocess.run(
            ["git", "merge-file", "-p", str(ours_f), str(base_f), str(theirs_f)],
            capture_output=True, text=True,
        )
        if proc.returncode < 0:
            return None, None
        merged_lines = proc.stdout.split("\n")
        if merged_lines and merged_lines[-1] == "":
            merged_lines = merged_lines[:-1]
        if proc.returncode > 0:
            return None, "\n".join(merged_lines)
        return merged_lines, None


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

    merged_lines, conflict_text = _merge_file(ours_block, old_image, hunk.new_image)
    if merged_lines is None:
        return None, conflict_text
    return current_lines[:start] + merged_lines + current_lines[start + n:], None


def _function_name_from_section(section: str) -> str | None:
    """Extract a function name from git's hunk-section text.

    Hunk headers preserve the declaration text nearest to the edit, which
    makes them a useful, patch-native anchor.  A section such as ``static
    int foo(struct bar *x)`` yields ``foo``.  Sections for includes,
    structs, and macro-heavy declarations deliberately return no anchor.
    """
    names = re.findall(r"\b([A-Za-z_]\w*)\s*\(", section)
    return names[-1] if names else None


def _function_anchors(current_lines: list[str], function_name: str) -> list[tuple[int, int]] | None:
    """Return line spans of C functions named ``function_name``.

    ``None`` means tree-sitter is unavailable, while an empty list means the
    parser ran but found no unambiguous C function.  Keeping the dependency
    optional preserves Loom's base install: Tier 0/1 continue to work and a
    Tier-2 candidate is reported unresolved rather than guessed at.
    """
    try:
        from tree_sitter import Language, Parser
        import tree_sitter_c
    except ImportError:
        return None

    source = "\n".join(current_lines).encode()
    parser = Parser(Language(tree_sitter_c.language()))
    tree = parser.parse(source)
    anchors: list[tuple[int, int]] = []

    def visit(node) -> None:
        if node.type != "function_definition":
            for child in node.children:
                visit(child)
            return
        declarator = node.child_by_field_name("declarator")
        if declarator is not None:
            declarator_text = source[declarator.start_byte:declarator.end_byte].decode(errors="replace")
            # The declarator can contain parameter names too; the first name
            # immediately followed by an opening parenthesis is the function.
            match = re.search(r"\b([A-Za-z_]\w*)\s*\(", declarator_text)
            if match and match.group(1) == function_name:
                # Byte offsets are stable across the supported tree-sitter
                # bindings.  Deriving line spans from them also avoids
                # depending on Point's binding-specific representation.
                start_line = source.count(b"\n", 0, node.start_byte)
                end_line = source.count(b"\n", 0, node.end_byte) + 1
                anchors.append((start_line, end_line))

    visit(tree.root_node)
    return anchors


def _try_tier2(
    current_lines: list[str], hunk: Hunk, is_c_source: bool,
) -> tuple[list[str] | None, str | None, str]:
    """Relocate a hunk into its uniquely named C function and merge it.

    The final operation remains the same conservative three-way merge as
    tier 1.  Tree-sitter only narrows the candidate region after the local
    window was exhausted, so a function that moved hundreds or thousands of
    lines can be handled without widening Tier 1 into a risky whole-file
    search.
    """
    if not is_c_source:
        return None, None, "Tier 2 only anchors C source/header files"
    function_name = _function_name_from_section(hunk.section)
    if not function_name:
        return None, None, "hunk has no function signature usable as a C anchor"
    anchors = _function_anchors(current_lines, function_name)
    if anchors is None:
        return None, None, "tree-sitter C parser is not installed (install susfs-loom[tier2])"
    if len(anchors) != 1:
        return None, None, (
            f"C function anchor '{function_name}' was "
            f"{'not found' if not anchors else 'ambiguous'}"
        )

    start, end = anchors[0]
    old_image = hunk.old_image
    if not old_image:
        return None, None, "empty old-image cannot be safely anchored"
    # The AST node is the safety boundary now, so searching all of this one
    # function is safe even when it is longer than Tier 1's 400-line window.
    located = _locate_best_window(
        current_lines[start:end], old_image, approx_start=0, search_window=None,
    )
    if located is None or located[1] < MATCH_THRESHOLD:
        return None, None, f"no plausible context match inside C function '{function_name}'"
    relative_start, _ratio = located
    absolute_start = start + relative_start
    ours_block = current_lines[absolute_start:absolute_start + len(old_image)]

    if not shutil.which("git"):
        return None, None, "git is unavailable for the required 3-way merge"
    merged_lines, conflict_text = _merge_file(ours_block, old_image, hunk.new_image)
    if merged_lines is None:
        if conflict_text is None:
            return None, None, "git merge-file failed"
        return None, conflict_text, f"3-way merge conflicted inside C function '{function_name}'"
    return current_lines[:absolute_start] + merged_lines + current_lines[absolute_start + len(old_image):], None, (
        f"relocated via unique C function anchor '{function_name}'"
    )


def _handoff_candidate(current_lines: list[str], hunk: Hunk, offset: int) -> tuple[str, int]:
    """Return a small, reviewable target region for an unresolved hunk.

    This is deliberately diagnostic only: it never affects resolution or
    writing.  Prefer Tier 1's locally best textual candidate; if there is no
    credible candidate, show the recorded location with surrounding lines.
    """
    old_image = hunk.old_image
    approx_start = max(0, hunk.old_start - 1 + offset)
    located = _locate_best_window(current_lines, old_image, approx_start) if old_image else None
    start = located[0] if located else min(approx_start, len(current_lines))
    context_start = max(0, start - 3)
    context_end = min(len(current_lines), start + max(len(old_image), 1) + 3)
    return "\n".join(current_lines[context_start:context_end]), context_start + 1


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

            spliced, tier1_conflict = _try_tier1(current_lines, hunk, offset)
            if spliced is not None:
                offset += len(spliced) - len(current_lines)
                current_lines = spliced
                report.results.append(HunkResult(
                    file=rel, hunk_index=i, section=hunk.section,
                    tier=1, status="applied",
                ))
                continue

            spliced, tier2_conflict, tier2_detail = _try_tier2(
                current_lines, hunk, Path(rel).suffix in {".c", ".h"},
            )
            if spliced is not None:
                offset += len(spliced) - len(current_lines)
                current_lines = spliced
                report.results.append(HunkResult(
                    file=rel, hunk_index=i, section=hunk.section,
                    tier=2, status="applied", detail=tier2_detail,
                ))
                continue

            file_had_unresolved = True
            conflict_text = tier2_conflict or tier1_conflict
            status = "conflict" if conflict_text else "unresolved"
            detail = (
                f"{tier2_detail}; genuine overlap needs tier 4 (human handoff, not built)"
                if conflict_text else
                f"{tier2_detail}; needs tier 3 (semantic patch, not built) or tier 4 (human handoff, not built)"
            )
            handoff_text, handoff_start = (
                (conflict_text, None) if conflict_text else _handoff_candidate(current_lines, hunk, offset)
            )
            report.results.append(HunkResult(
                file=rel, hunk_index=i, section=hunk.section,
                tier=None, status=status, detail=detail, conflict_text=conflict_text,
                handoff_text=handoff_text, handoff_start=handoff_start,
                patch_old_text="\n".join(hunk.old_image),
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
