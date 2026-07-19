# verify tests use small synthetic patch/tree pairs (unlike test_cascade.py's
# use of the real susfs patch fixture) because verify's job is to react to
# the *result* left on disk, not to patch-application mechanics - a
# synthetic before/after pair exercises every status (ok / missing-macros /
# syntax-error / not-found) far more directly than fishing for each case out
# of the one real fixture.
from pathlib import Path

import pytest

from loom.report import verify_to_text
from loom.verify import verify_tree

PATCH_TEXT = (
    "diff --git a/fs/demo.c b/fs/demo.c\n"
    "--- a/fs/demo.c\n"
    "+++ b/fs/demo.c\n"
    "@@ -1,3 +1,6 @@\n"
    " static int demo(void) {\n"
    "+#ifdef CONFIG_KSU_SUSFS_SUS_PATH\n"
    "+    susfs_sus_path_hook();\n"
    "+#endif\n"
    "     return 0;\n"
    " }\n"
)


@pytest.fixture
def patch_file(tmp_path):
    p = tmp_path / "demo.patch"
    p.write_text(PATCH_TEXT)
    return p


def _write(tree: Path, rel: str, text: str) -> None:
    f = tree / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)


def test_verify_ok_when_macro_present_and_syntax_balanced(tmp_path, patch_file):
    _write(tmp_path, "fs/demo.c", (
        "static int demo(void) {\n"
        "#ifdef CONFIG_KSU_SUSFS_SUS_PATH\n"
        "    susfs_sus_path_hook();\n"
        "#endif\n"
        "    return 0;\n"
        "}\n"
    ))
    report = verify_tree(tmp_path, patch_file)
    assert len(report.results) == 1
    r = report.results[0]
    assert r.status == "ok"
    assert r.expected_macros == ["CONFIG_KSU_SUSFS_SUS_PATH"]
    assert r.missing_macros == []
    assert r.syntax_ok is True
    assert not report.failed


def test_verify_flags_missing_macro(tmp_path, patch_file):
    # the hook never made it into the file - same shape of bug a wrong
    # tier-1 splice location could produce even while reporting "applied"
    _write(tmp_path, "fs/demo.c", (
        "static int demo(void) {\n"
        "    return 0;\n"
        "}\n"
    ))
    report = verify_tree(tmp_path, patch_file)
    r = report.results[0]
    assert r.status == "missing-macros"
    assert r.missing_macros == ["CONFIG_KSU_SUSFS_SUS_PATH"]
    assert r in report.failed


def test_verify_flags_unbalanced_braces(tmp_path, patch_file):
    # simulates a bad splice: opening brace with no matching close
    _write(tmp_path, "fs/demo.c", (
        "static int demo(void) {\n"
        "#ifdef CONFIG_KSU_SUSFS_SUS_PATH\n"
        "    susfs_sus_path_hook();\n"
        "#endif\n"
        "    return 0;\n"
    ))
    report = verify_tree(tmp_path, patch_file)
    r = report.results[0]
    assert r.status == "syntax-error"
    assert r.syntax_ok is False
    # backend depends on whether tree-sitter-c is installed - both report
    # a parse problem, just in different words, so accept either.
    assert r.syntax_method in ("tree-sitter", "brace-balance")
    assert r.syntax_detail


def test_verify_reports_not_found_for_missing_file(tmp_path, patch_file):
    report = verify_tree(tmp_path, patch_file)  # fs/demo.c never written
    r = report.results[0]
    assert r.status == "not-found"
    assert r in report.failed


def test_verify_ignores_macro_tokens_inside_comments_and_strings_for_balance_only(tmp_path, patch_file):
    # braces inside a string/comment must not confuse the balance scanner
    _write(tmp_path, "fs/demo.c", (
        "static int demo(void) {\n"
        "#ifdef CONFIG_KSU_SUSFS_SUS_PATH\n"
        '    /* unbalanced { on purpose in a comment */\n'
        '    char *s = "also { unbalanced in a string";\n'
        "    susfs_sus_path_hook();\n"
        "#endif\n"
        "    return 0;\n"
        "}\n"
    ))
    report = verify_tree(tmp_path, patch_file)
    r = report.results[0]
    assert r.syntax_ok is True
    assert r.status == "ok"


def test_verify_to_text_renders_summary(tmp_path, patch_file):
    _write(tmp_path, "fs/demo.c", (
        "static int demo(void) {\n"
        "    return 0;\n"
        "}\n"
    ))
    report = verify_tree(tmp_path, patch_file)
    text = verify_to_text(report)
    assert "fs/demo.c" in text
    assert "missing-macros" in text
    assert "failed verification" in text


def test_verify_non_c_files_skip_syntax_check(tmp_path):
    patch = tmp_path / "kbuild.patch"
    patch.write_text(
        "diff --git a/fs/Makefile b/fs/Makefile\n"
        "--- a/fs/Makefile\n"
        "+++ b/fs/Makefile\n"
        "@@ -1,2 +1,3 @@\n"
        " obj-y += open.o\n"
        "+obj-$(CONFIG_KSU_SUSFS) += susfs.o\n"
        " obj-y += read_write.o\n"
    )
    _write(tmp_path, "fs/Makefile", "obj-y += open.o\nobj-$(CONFIG_KSU_SUSFS) += susfs.o\nobj-y += read_write.o\n")
    report = verify_tree(tmp_path, patch)
    r = report.results[0]
    assert r.syntax_ok is None
    assert r.syntax_method.startswith("skipped")
    assert r.status == "ok"
