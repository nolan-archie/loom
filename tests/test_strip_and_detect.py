# tests run against a real susfs4ksu patch, not made-up examples (see
# fixtures/). the fs/namespace.c snippets below are copied straight out of
# the real hunks - import block, mnt_free_id, mnt_alloc_group_id (the goto
# case). didn't want to fake this one, the whole point of strip is that it
# has to survive real code.
from pathlib import Path

import pytest

from loom.detect import detect
from loom.strip import apply_strip, strip_tree

FIXTURES = Path(__file__).parent / "fixtures"
REAL_PATCH = FIXTURES / "50_add_susfs_in_gki-android14-6.1.patch"

HOOKED_NAMESPACE_C = '''\
#include <linux/fs_context.h>
#include <linux/shmem_fs.h>
#include <linux/mnt_idmapping.h>
#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
#include <linux/susfs_def.h>
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT

#include "pnode.h"
#include "internal.h"

#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
extern bool susfs_is_current_ksu_domain(void);
extern struct static_key_true susfs_is_sdcard_android_data_not_decrypted;

#define CL_COPY_MNT_NS BIT(25) /* used by copy_mnt_ns() */

#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT

/* Maximum number of mounts in a mount namespace */
static unsigned int sysctl_mount_max __read_mostly = 100000;

static void mnt_free_id(struct mount *mnt)
{
#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
\tif (mnt->mnt.mnt_flags & VFSMOUNT_MNT_FLAGS_KSU_UNSHARED_MNT)
\t\treturn;

#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
\tida_free(&mnt_id_ida, mnt->mnt_id);
}

static int mnt_alloc_group_id(struct mount *mnt)
{
#ifdef CONFIG_KSU_SUSFS_SUS_MOUNT
\tint res;

\tif (susfs_is_current_ksu_domain()) {
\t\tres = ida_alloc_min(&mnt_group_ida, DEFAULT_KSU_MNT_GROUP_ID, GFP_KERNEL);
\t\tgoto bypass_orig_flow;
\t}

\tres = ida_alloc_min(&mnt_group_ida, 1, GFP_KERNEL);
bypass_orig_flow:
#else
\tint res = ida_alloc_min(&mnt_group_ida, 1, GFP_KERNEL);
#endif // #ifdef CONFIG_KSU_SUSFS_SUS_MOUNT

\tif (res < 0)
\t\treturn res;
\tmnt->mnt_group_id = res;
\treturn 0;
}
'''

EXPECTED_ORIGINAL_NAMESPACE_C = '''\
#include <linux/fs_context.h>
#include <linux/shmem_fs.h>
#include <linux/mnt_idmapping.h>

#include "pnode.h"
#include "internal.h"


/* Maximum number of mounts in a mount namespace */
static unsigned int sysctl_mount_max __read_mostly = 100000;

static void mnt_free_id(struct mount *mnt)
{
\tida_free(&mnt_id_ida, mnt->mnt_id);
}

static int mnt_alloc_group_id(struct mount *mnt)
{
\tint res = ida_alloc_min(&mnt_group_ida, 1, GFP_KERNEL);

\tif (res < 0)
\t\treturn res;
\tmnt->mnt_group_id = res;
\treturn 0;
}
'''

HOOKED_MAKEFILE = """\
obj-y :=\topen.o read_write.o file_table.o super.o \\
\t\tfs_types.o fs_context.o fs_parser.o fsopen.o init.o \\
\t\tkernel_read_file.o remap_range.o

obj-$(CONFIG_KSU_SUSFS) += susfs.o

ifeq ($(CONFIG_BLOCK),y)
obj-y += buffer.o
endif
"""

EXPECTED_ORIGINAL_MAKEFILE = """\
obj-y :=\topen.o read_write.o file_table.o super.o \\
\t\tfs_types.o fs_context.o fs_parser.o fsopen.o init.o \\
\t\tkernel_read_file.o remap_range.o


ifeq ($(CONFIG_BLOCK),y)
obj-y += buffer.o
endif
"""


@pytest.fixture
def hooked_tree(tmp_path):
    (tmp_path / "fs").mkdir()
    (tmp_path / "fs" / "namespace.c").write_text(HOOKED_NAMESPACE_C)
    (tmp_path / "fs" / "Makefile").write_text(HOOKED_MAKEFILE)
    return tmp_path


def test_real_patch_macro_extraction():
    # make sure the regex actually finds susfs's real macro set and not
    # just whatever subset I happened to hardcode while testing
    from loom.patchutils import load_patch

    info = load_patch(REAL_PATCH)
    assert "CONFIG_KSU_SUSFS" in info.macros
    assert "CONFIG_KSU_SUSFS_SUS_MOUNT" in info.macros
    assert "CONFIG_KSU_SUSFS_SUS_PATH" in info.macros
    assert "fs/namespace.c" in info.touched_files
    assert "fs/Makefile" in info.touched_files


def test_strip_reconstructs_goto_label_case_byte_exact(hooked_tree):
    # this is the mnt_alloc_group_id case - goto label defined inside the
    # guarded branch, which is the trickiest of the three patterns I checked
    report = strip_tree(hooked_tree, REAL_PATCH)
    results_by_path = {r.path: r for r in report.results}

    ns = results_by_path["fs/namespace.c"]
    assert ns.status == "stripped"
    assert ns.new_text == EXPECTED_ORIGINAL_NAMESPACE_C


def test_strip_kbuild_conditional_line_removed(hooked_tree):
    report = strip_tree(hooked_tree, REAL_PATCH)
    mk = {r.path: r for r in report.results}["fs/Makefile"]
    assert mk.kind == "kbuild"
    assert mk.status == "stripped"
    assert mk.new_text == EXPECTED_ORIGINAL_MAKEFILE


def test_missing_files_reported_not_silently_skipped(hooked_tree):
    report = strip_tree(hooked_tree, REAL_PATCH)
    missing = [r for r in report.results if r.status == "not-found"]
    # the real patch touches 23 files; only 2 exist in our synthetic tree
    assert len(missing) == 21
    assert all(r.new_text is None for r in missing)


def test_coverage_check_flags_unguarded_leftover_token(tmp_path):
    # simulates someone hand-fixing a .rej and forgetting to put the ifdef back
    (tmp_path / "fs").mkdir()
    (tmp_path / "fs" / "namespace.c").write_text(
        "static void mnt_free_id(struct mount *mnt)\n"
        "{\n"
        "\tif (susfs_is_current_ksu_domain())\n"
        "\t\treturn;\n"
        "\tida_free(&mnt_id_ida, mnt->mnt_id);\n"
        "}\n"
    )
    report = strip_tree(tmp_path, REAL_PATCH)
    ns = {r.path: r for r in report.results}["fs/namespace.c"]
    assert ns.status == "coverage-flag"
    assert len(ns.surviving_tokens) == 1
    assert report.needs_review == [ns]


def test_apply_skips_flagged_files_by_default(tmp_path):
    original = (
        "static void mnt_free_id(struct mount *mnt)\n"
        "{\n"
        "\tif (susfs_is_current_ksu_domain())\n"
        "\t\treturn;\n"
        "\tida_free(&mnt_id_ida, mnt->mnt_id);\n"
        "}\n"
    )
    (tmp_path / "fs").mkdir()
    f = tmp_path / "fs" / "namespace.c"
    f.write_text(original)
    report = strip_tree(tmp_path, REAL_PATCH)
    written = apply_strip(report, tmp_path)  # skip_flagged=True by default
    assert written == []
    assert f.read_text() == original  # untouched on disk


def test_apply_writes_clean_files(hooked_tree):
    report = strip_tree(hooked_tree, REAL_PATCH)
    written = apply_strip(report, hooked_tree)
    assert set(written) == {"fs/namespace.c", "fs/Makefile"}
    assert (hooked_tree / "fs" / "namespace.c").read_text() == EXPECTED_ORIGINAL_NAMESPACE_C


def test_strip_raises_on_patch_with_no_susfs_macros(tmp_path):
    bad_patch = tmp_path / "unrelated.patch"
    bad_patch.write_text("diff --git a/foo.c b/foo.c\n+int x;\n")
    with pytest.raises(ValueError, match="no CONFIG_KSU_SUSFS"):
        strip_tree(tmp_path, bad_patch)


# --- Detect (Stage 0) ---

def test_detect_kernel_version_and_gki_branch(tmp_path):
    (tmp_path / "Makefile").write_text(
        "VERSION = 6\nPATCHLEVEL = 1\nSUBLEVEL = 25\nEXTRAVERSION =\nNAME = Curry Ramen\n"
    )
    (tmp_path / "build.config.gki").write_text("BRANCH=android14-6.1\n")
    result = detect(tmp_path)
    assert result.kernel.short == "6.1.25"
    assert result.kernel.gki_branch_guess == "android14-6.1"


def test_detect_susfs_presence_and_macro_set(hooked_tree):
    result = detect(hooked_tree)
    assert result.susfs.present is True
    assert "CONFIG_KSU_SUSFS_SUS_MOUNT" in result.susfs.macros_found


def test_detect_no_susfs_on_clean_tree(tmp_path):
    (tmp_path / "fs").mkdir()
    (tmp_path / "fs" / "namespace.c").write_text("int x;\n")
    result = detect(tmp_path)
    assert result.susfs.present is False
    assert result.susfs.macros_found == []


def test_detect_ksu_fork_signature_match_kernelsu_next(tmp_path):
    ksu = tmp_path / "KernelSU" / "kernel"
    ksu.mkdir(parents=True)
    (ksu / "Kconfig").touch()
    (ksu / "extras.c").touch()
    result = detect(tmp_path)
    assert result.ksu_fork.fork == "KernelSU-Next"
    assert result.ksu_fork.confidence == "signature-match"


def test_detect_ksu_fork_baseline_when_no_marker_matches(tmp_path):
    ksu = tmp_path / "KernelSU" / "kernel"
    ksu.mkdir(parents=True)
    (ksu / "Kconfig").touch()
    result = detect(tmp_path)
    assert result.ksu_fork.confidence == "baseline-default"


def test_detect_no_kernelsu_dir(tmp_path):
    result = detect(tmp_path)
    assert result.ksu_fork.present is False
