# cascade tests use the same real susfs4ksu patch as test_strip_and_detect.py
# (see that file's header comment for why) plus synthetic trees built to
# exercise specific drift scenarios - unlike strip's ifdef reconstruction,
# there's no single real fixture that demonstrates "vendor line shifted my
# hunk's offset" vs. "vendor genuinely fought susfs for the same insertion
# point", so those two are deliberately constructed here.
from pathlib import Path

import pytest

from loom.cascade import apply_cascade, cascade_apply, parse_hunks

FIXTURES = Path(__file__).parent / "fixtures"
REAL_PATCH = FIXTURES / "50_add_susfs_in_gki-android14-6.1.patch"

# lines 17-24 of the real upstream fs/Makefile that 50_add_susfs_in_gki-
# android14-6.1.patch's hunk (@@ -17,6 +17,8 @@) expects to find, copied
# verbatim from the patch's own context/old-image lines - not guessed.
MAKEFILE_CONTEXT = [
    "\t\tfs_types.o fs_context.o fs_parser.o fsopen.o init.o \\",
    "\t\tkernel_read_file.o remap_range.o",
    "",
    "ifeq ($(CONFIG_BLOCK),y)",
    "obj-y +=\tbuffer.o direct-io.o mpage.o",
    "else",
]
MAKEFILE_TAIL = ["obj-y += buffer.o", "endif"]
FILLER = [f"placeholder_line_{i}" for i in range(1, 17)]  # lines 1-16, unused by the hunk


def _write_makefile(tree: Path, lines: list[str]) -> Path:
    (tree / "fs").mkdir(exist_ok=True)
    f = tree / "fs" / "Makefile"
    f.write_text("\n".join(lines) + "\n")
    return f


@pytest.fixture
def makefile_tree(tmp_path):
    _write_makefile(tmp_path, FILLER + MAKEFILE_CONTEXT + MAKEFILE_TAIL)
    return tmp_path


def test_parse_hunks_matches_real_patch_structure():
    # spot-check against known-real hunk boundaries so a parser bug doesn't
    # silently corrupt hunk boundaries without any test noticing
    file_diffs = parse_hunks(REAL_PATCH.read_text())
    by_path = {fd.new_path: fd for fd in file_diffs}

    makefile = by_path["fs/Makefile"]
    assert len(makefile.hunks) == 1
    h = makefile.hunks[0]
    assert (h.old_start, h.old_count, h.new_start, h.new_count) == (17, 6, 17, 8)
    assert h.old_image == MAKEFILE_CONTEXT
    assert "obj-$(CONFIG_KSU_SUSFS) += susfs.o" in h.new_image

    namespace = by_path["fs/namespace.c"]
    assert len(namespace.hunks) == 9  # real patch, not a made-up count


def test_tier0_exact_match_on_untouched_tree(makefile_tree):
    # clean fresh-wire case: tree matches exactly what the patch expects,
    # no drift at all - should resolve at tier 0 without needing merge-file
    report = cascade_apply(makefile_tree, REAL_PATCH)
    results = [r for r in report.results if r.file == "fs/Makefile"]
    assert len(results) == 1
    assert results[0].tier == 0
    assert results[0].status == "applied"
    assert "fs/Makefile" in report.resolved_file_text
    assert "obj-$(CONFIG_KSU_SUSFS) += susfs.o" in report.resolved_file_text["fs/Makefile"]


def test_tier1_resolves_unrelated_line_drift(tmp_path):
    # vendor inserted unrelated lines above the hook site - shifts the
    # hunk's real location away from the line number the patch recorded,
    # but the context itself is untouched. tier 0 must fail here (wrong
    # offset) and tier 1 must succeed (same content, found elsewhere).
    drifted = FILLER + ["vendor_added_1", "vendor_added_2", "vendor_added_3"] + MAKEFILE_CONTEXT + MAKEFILE_TAIL
    _write_makefile(tmp_path, drifted)

    report = cascade_apply(tmp_path, REAL_PATCH)
    result = next(r for r in report.results if r.file == "fs/Makefile")
    assert result.tier == 1
    assert result.status == "applied"
    assert "fs/Makefile" in report.resolved_file_text
    # the vendor's own unrelated lines must survive untouched in the result
    assert "vendor_added_2" in report.resolved_file_text["fs/Makefile"]
    assert "obj-$(CONFIG_KSU_SUSFS) += susfs.o" in report.resolved_file_text["fs/Makefile"]


def test_genuine_conflict_is_not_silently_resolved(tmp_path):
    # vendor added its OWN kbuild line at the exact same insertion point
    # susfs wants - a real logical collision, not just drift. must NOT be
    # silently resolved by either tier.
    conflicting = FILLER + [
        "\t\tfs_types.o fs_context.o fs_parser.o fsopen.o init.o \\",
        "\t\tkernel_read_file.o remap_range.o",
        "",
        "obj-$(CONFIG_VENDOR_HOOK) += vendor_hook.o",
        "",
        "ifeq ($(CONFIG_BLOCK),y)",
        "obj-y +=\tbuffer.o direct-io.o mpage.o",
        "else",
    ] + MAKEFILE_TAIL
    _write_makefile(tmp_path, conflicting)

    report = cascade_apply(tmp_path, REAL_PATCH)
    result = next(r for r in report.results if r.file == "fs/Makefile")
    assert result.tier is None
    assert result.status == "conflict"
    assert "<<<<<<<" in result.conflict_text
    assert "fs/Makefile" not in report.resolved_file_text


def test_file_not_found_reported_per_hunk_not_silently_skipped():
    report = cascade_apply(Path("/tmp"), REAL_PATCH)  # no fs/ dir at all
    missing = [r for r in report.results if r.file == "fs/Makefile"]
    assert len(missing) == 1
    assert missing[0].status == "file-not-found"
    assert missing[0].tier is None


def test_apply_cascade_only_writes_fully_resolved_files(makefile_tree):
    # everything in fs/Makefile resolves cleanly - should get written
    report = cascade_apply(makefile_tree, REAL_PATCH)
    written = apply_cascade(report, makefile_tree)
    assert written == ["fs/Makefile"]
    on_disk = (makefile_tree / "fs" / "Makefile").read_text()
    assert "obj-$(CONFIG_KSU_SUSFS) += susfs.o" in on_disk


def test_apply_cascade_never_partially_writes_a_conflicted_file(tmp_path):
    conflicting = FILLER + [
        "\t\tfs_types.o fs_context.o fs_parser.o fsopen.o init.o \\",
        "\t\tkernel_read_file.o remap_range.o",
        "",
        "obj-$(CONFIG_VENDOR_HOOK) += vendor_hook.o",
        "",
        "ifeq ($(CONFIG_BLOCK),y)",
        "obj-y +=\tbuffer.o direct-io.o mpage.o",
        "else",
    ] + MAKEFILE_TAIL
    original_text = "\n".join(conflicting) + "\n"
    f = _write_makefile(tmp_path, conflicting)

    report = cascade_apply(tmp_path, REAL_PATCH)
    written = apply_cascade(report, tmp_path)
    assert "fs/Makefile" not in written
    assert f.read_text() == original_text  # untouched on disk


def test_by_tier_count_breakdown(makefile_tree):
    report = cascade_apply(makefile_tree, REAL_PATCH)
    counts = report.by_tier_count
    assert counts["tier-0"] >= 1
    assert counts["unresolved"] >= 1  # every other file in the real patch is missing from this tree


def test_tier2_relocates_into_named_c_function_beyond_tier1_window(tmp_path):
    pytest.importorskip("tree_sitter")
    pytest.importorskip("tree_sitter_c")

    # The patch records target() near the start of the reference file, but
    # this downstream tree has accumulated 450 lines above it. Tier 1's
    # ±400-line textual search must refuse to reach that far; Tier 2 may use
    # the hunk header's function signature and the C AST to search target()
    # itself, then retains the same conservative 3-way merge operation.
    patch = tmp_path / "target.patch"
    patch.write_text(
        "diff --git a/demo.c b/demo.c\n"
        "--- a/demo.c\n"
        "+++ b/demo.c\n"
        "@@ -2,3 +2,4 @@ static int target(void)\n"
        " static int target(void) {\n"
        "     return 7;\n"
        " }\n"
        "+/* susfs hook */\n"
    )
    source = [f"/* downstream preamble {i} */" for i in range(450)] + [
        "static int target(void) {",
        "    return 7;",
        "}",
    ]
    (tmp_path / "demo.c").write_text("\n".join(source) + "\n")

    report = cascade_apply(tmp_path, patch)
    result = report.results[0]
    assert result.tier == 2
    assert result.status == "applied"
    assert "target" in result.detail
    assert "/* susfs hook */" in report.resolved_file_text["demo.c"]
    assert report.by_tier_count["tier-2"] == 1
