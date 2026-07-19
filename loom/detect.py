"""stage 0 - figure out what we're even looking at before touching anything.

kernel version, whether susfs is already in there, which KSU fork got
vendored in. fork detection is by file signature not by directory name,
since basically every fork gets dropped into a dir just called KernelSU/
regardless of upstream - so the name tells you nothing.

signatures below came from actually diffing the three main forks' repos,
they're not made up. definitely not exhaustive though, new forks show up
all the time and this list will go stale. unrecognized layout = "unknown",
we don't guess.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .patchutils import MACRO_RE

VERSION_FIELD_RE = re.compile(
    r"^(VERSION|PATCHLEVEL|SUBLEVEL|EXTRAVERSION)\s*=\s*(.*?)\s*$", re.MULTILINE
)
GKI_BRANCH_RE = re.compile(r"android\d{2}-\d+\.\d+")

SCAN_EXTS = {".c", ".h", "", "Makefile", "Kconfig", "Kbuild"}

# KernelSU-Next has an extra kernel/extras.c that upstream doesn't.
# SukiSU-Ultra bolts on a KPM subsystem + compat shim (kernel_compat.h, kpm/).
# checked in this order, first full match wins.
KSU_FORK_SIGNATURES: dict[str, list[str]] = {
    "KernelSU-Next": ["kernel/extras.c"],
    "SukiSU-Ultra": ["kernel/kernel_compat.h", "kernel/kpm"],
}


@dataclass
class KernelVersionInfo:
    version: str | None = None
    patchlevel: str | None = None
    sublevel: str | None = None
    extraversion: str | None = None
    gki_branch_guess: str | None = None
    evidence: str | None = None

    @property
    def short(self) -> str:
        if self.version and self.patchlevel:
            v = f"{self.version}.{self.patchlevel}"
            if self.sublevel:
                v += f".{self.sublevel}"
            return v
        return "unknown"


@dataclass
class SusfsState:
    present: bool = False
    macros_found: list[str] = field(default_factory=list)


@dataclass
class KsuForkInfo:
    present: bool = False
    fork: str | None = None
    confidence: str = "n/a"  # signature-match / baseline-default / unknown
    evidence: list[str] = field(default_factory=list)


@dataclass
class TreeFingerprint:
    # placeholder for the "hash of function signatures near hook points"
    # idea from the design doc - not computed yet, just here so the schema
    # doesn't need to change later when someone gets around to it
    hook_point_hashes: dict[str, str] = field(default_factory=dict)

    @property
    def implemented(self) -> bool:
        return False


@dataclass
class DetectResult:
    tree_path: str
    kernel: KernelVersionInfo
    susfs: SusfsState
    ksu_fork: KsuForkInfo
    fingerprint: TreeFingerprint


def _detect_kernel_version(tree: Path) -> KernelVersionInfo:
    makefile = tree / "Makefile"
    info = KernelVersionInfo()
    if makefile.is_file():
        # version fields are always near the top, no need to read the whole file
        text = makefile.read_text(errors="replace")[:4000]
        fields = dict(VERSION_FIELD_RE.findall(text))
        info.version = fields.get("VERSION") or None
        info.patchlevel = fields.get("PATCHLEVEL") or None
        info.sublevel = fields.get("SUBLEVEL") or None
        info.extraversion = fields.get("EXTRAVERSION") or None

    for c in [makefile] + sorted(tree.glob("build.config*")):
        if not c.is_file():
            continue
        m = GKI_BRANCH_RE.search(c.read_text(errors="replace"))
        if m:
            info.gki_branch_guess = m.group(0)
            info.evidence = str(c.relative_to(tree))
            break

    return info


def _iter_scan_files(tree: Path):
    for p in tree.rglob("*"):
        if not p.is_file() or ".git" in p.parts:
            continue
        if p.suffix in SCAN_EXTS or p.name in ("Makefile", "Kconfig", "Kbuild"):
            yield p


def _detect_susfs_state(tree: Path) -> SusfsState:
    found = set()
    for p in _iter_scan_files(tree):
        try:
            found.update(MACRO_RE.findall(p.read_text(errors="replace")))
        except OSError:
            pass
    return SusfsState(present=bool(found), macros_found=sorted(found))


def _detect_ksu_fork(tree: Path) -> KsuForkInfo:
    ksu_dir = tree / "KernelSU"
    if not ksu_dir.is_dir():
        return KsuForkInfo(present=False)

    for fork_name, markers in KSU_FORK_SIGNATURES.items():
        marker_paths = [ksu_dir / m for m in markers]
        if all(mp.exists() for mp in marker_paths):
            return KsuForkInfo(
                present=True,
                fork=fork_name,
                confidence="signature-match",
                evidence=[str(mp.relative_to(tree)) for mp in marker_paths],
            )

    # nothing matched but KernelSU/ has the expected layout - probably just
    # unmodified upstream, but this is a default not a real ID
    if (ksu_dir / "kernel" / "Kconfig").is_file():
        return KsuForkInfo(
            present=True,
            fork="tiann/KernelSU (baseline assumption)",
            confidence="baseline-default",
            evidence=["no known fork marker matched, assuming unmodified upstream"],
        )

    return KsuForkInfo(
        present=True, fork=None, confidence="unknown",
        evidence=["KernelSU/ exists but layout doesn't match anything we know"],
    )


def detect(tree_path: str | Path) -> DetectResult:
    tree = Path(tree_path)
    if not tree.is_dir():
        raise NotADirectoryError(f"{tree_path} is not a directory")
    return DetectResult(
        tree_path=str(tree),
        kernel=_detect_kernel_version(tree),
        susfs=_detect_susfs_state(tree),
        ksu_fork=_detect_ksu_fork(tree),
        fingerprint=TreeFingerprint(),
    )
