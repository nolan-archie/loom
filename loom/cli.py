from __future__ import annotations

import argparse
import sys

from . import __version__
from .cascade import apply_cascade, cascade_apply
from .detect import detect
from .report import (
    cascade_to_json,
    cascade_to_text,
    detect_to_json,
    detect_to_text,
    strip_to_json,
    strip_to_text,
    verify_to_json,
    verify_to_text,
)
from .strip import apply_strip, strip_tree
from .verify import verify_tree


def cmd_detect(args: argparse.Namespace) -> int:
    result = detect(args.kernel_tree)
    print(detect_to_json(result) if args.json else detect_to_text(result))
    return 0


def cmd_strip(args: argparse.Namespace) -> int:
    try:
        report = strip_tree(args.kernel_tree, args.new_susfs_patch)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.apply:
        written = apply_strip(report, args.kernel_tree, skip_flagged=not args.force)
        if not args.json:
            print(f"wrote {len(written)} file(s) back to {args.kernel_tree}\n")

    print(strip_to_json(report) if args.json else strip_to_text(report))
    return 1 if report.needs_review and not args.force else 0


def cmd_verify(args: argparse.Namespace) -> int:
    report = verify_tree(args.kernel_tree, args.susfs_patch, compile_check=args.compile)
    print(verify_to_json(report) if args.json else verify_to_text(report))
    return 1 if report.failed else 0


def _maybe_verify_after_apply(args: argparse.Namespace, patch_path: str) -> bool:
    """runs stage 3 after a successful --apply, if --verify was passed.
    returns True iff verify ran and found a failure - callers fold this
    into their own exit code rather than overriding it, since a wire/
    restage run that resolved every hunk but then fails verify is still
    informative as a non-zero exit."""
    if not (args.apply and args.verify):
        return False
    report = verify_tree(args.kernel_tree, patch_path, compile_check=args.compile)
    print("\n--- stage 3: verify ---\n" if not args.json else "", end="")
    print(verify_to_json(report) if args.json else verify_to_text(report))
    return bool(report.failed)


def cmd_wire(args: argparse.Namespace) -> int:
    # fresh wire = cascade engine directly against the target tree. tiers
    # 0-2 are built; anything they can't resolve is reported, not guessed.
    report = cascade_apply(args.kernel_tree, args.susfs_patch)

    if args.apply:
        written = apply_cascade(report, args.kernel_tree)
        if not args.json:
            print(f"wrote {len(written)} file(s) back to {args.kernel_tree}\n")

    print(cascade_to_json(report) if args.json else cascade_to_text(report, args.handoff))

    if not args.json and report.unresolved:
        print(
            "\nnote: loom wire currently runs tiers 0-2 (exact, 3-way, AST anchor). "
            "hunks listed above as unresolved need tier 3/4, which aren't built yet.",
            file=sys.stderr,
        )
    verify_failed = _maybe_verify_after_apply(args, args.susfs_patch)
    return 1 if (report.unresolved or verify_failed) else 0


def cmd_restage(args: argparse.Namespace) -> int:
    # restage = strip, then hand the now-clean-ish tree to the same cascade
    # engine fresh wire uses (design doc §4: "Strip, then Fresh Wire").
    try:
        strip_report = strip_tree(args.kernel_tree, args.new_susfs_patch)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if strip_report.needs_review and not args.force:
        print(
            "loom restage: strip stage flagged file(s) needing a human look "
            "before cascade can safely run - see below. re-run with --force "
            "on `loom strip` yourself first if you've resolved them by hand, "
            "or pass --force here to strip+cascade anyway (not recommended).",
            file=sys.stderr,
        )
        print(strip_to_text(strip_report), file=sys.stderr)
        return 1

    apply_strip(strip_report, args.kernel_tree, skip_flagged=not args.force)

    cascade_report = cascade_apply(args.kernel_tree, args.new_susfs_patch)
    if args.apply:
        written = apply_cascade(cascade_report, args.kernel_tree)
        if not args.json:
            print(f"wrote {len(written)} file(s) back to {args.kernel_tree}\n")

    print(cascade_to_json(cascade_report) if args.json else cascade_to_text(cascade_report, args.handoff))

    if not args.json and cascade_report.unresolved:
        print(
            "\nnote: loom restage currently runs cascade tiers 0-2. "
            "hunks listed above as unresolved need tier 3/4, which aren't built yet.",
            file=sys.stderr,
        )
    verify_failed = _maybe_verify_after_apply(args, args.new_susfs_patch)
    return 1 if (cascade_report.unresolved or verify_failed) else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="loom", description="patch-engine for susfs4ksu kernel integration")
    p.add_argument("--version", action="version", version=f"loom {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    p_detect = sub.add_parser("detect", help="fingerprint a tree - kernel version, susfs state, KSU fork")
    p_detect.add_argument("kernel_tree")
    p_detect.add_argument("--json", action="store_true")
    p_detect.set_defaults(func=cmd_detect)

    p_strip = sub.add_parser("strip", help="reconstruct pre-hook source using the new patch's macro list")
    p_strip.add_argument("kernel_tree")
    p_strip.add_argument("new_susfs_patch", help="the patch you're ABOUT to apply, not the old one")
    p_strip.add_argument("--apply", action="store_true", help="write to disk (default is dry run)")
    p_strip.add_argument("--force", action="store_true", help="also write files the coverage check flagged")
    p_strip.add_argument("--json", action="store_true")
    p_strip.set_defaults(func=cmd_strip)

    p_wire = sub.add_parser(
        "wire", help="fresh wire mode - cascade tiers 0-2 (3-4 not built)"
    )
    p_wire.add_argument("kernel_tree")
    p_wire.add_argument("susfs_patch")
    p_wire.add_argument("--apply", action="store_true", help="write to disk (default is dry run)")
    p_wire.add_argument("--json", action="store_true")
    p_wire.add_argument("--handoff", action="store_true", help="show Tier 4 review context for unresolved hunks")
    p_wire.add_argument("--verify", action="store_true", help="run stage 3 verify after --apply")
    p_wire.add_argument("--compile", action="store_true", help="with --verify, also try cc -fsyntax-only (advisory)")
    p_wire.set_defaults(func=cmd_wire)

    p_restage = sub.add_parser(
        "restage", help="strip + fresh wire - cascade tiers 0-2 (3-4 not built)"
    )
    p_restage.add_argument("kernel_tree")
    p_restage.add_argument("new_susfs_patch")
    p_restage.add_argument("--apply", action="store_true", help="write to disk (default is dry run)")
    p_restage.add_argument(
        "--force", action="store_true",
        help="also strip coverage-flagged files and cascade anyway (not recommended)",
    )
    p_restage.add_argument("--json", action="store_true")
    p_restage.add_argument("--handoff", action="store_true", help="show Tier 4 review context for unresolved hunks")
    p_restage.add_argument("--verify", action="store_true", help="run stage 3 verify after --apply")
    p_restage.add_argument("--compile", action="store_true", help="with --verify, also try cc -fsyntax-only (advisory)")
    p_restage.set_defaults(func=cmd_restage)

    p_verify = sub.add_parser(
        "verify", help="stage 3 - confirm expected macros + syntactic sanity post-apply"
    )
    p_verify.add_argument("kernel_tree")
    p_verify.add_argument("susfs_patch", help="the patch that was applied - used to know what to expect")
    p_verify.add_argument("--compile", action="store_true", help="also try cc -fsyntax-only per file (advisory)")
    p_verify.add_argument("--json", action="store_true")
    p_verify.set_defaults(func=cmd_verify)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
