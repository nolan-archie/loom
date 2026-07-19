from __future__ import annotations

import argparse
import sys

from . import __version__
from .detect import detect
from .report import detect_to_json, detect_to_text, strip_to_json, strip_to_text
from .strip import apply_strip, strip_tree


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


def cmd_wire(args: argparse.Namespace) -> int:
    print(
        "loom wire: not built yet. this needs the cascade engine (tiers 0-4), "
        "which is still on paper. detect + strip work, that's it for now.",
        file=sys.stderr,
    )
    return 2


def cmd_restage(args: argparse.Namespace) -> int:
    print(
        "loom restage: the strip half works fine (`loom strip`), but the "
        "cascade engine it hands off to isn't built. run strip yourself, "
        "then apply the new patch by hand against the stripped tree for now.",
        file=sys.stderr,
    )
    return 2


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

    p_wire = sub.add_parser("wire", help="fresh wire mode - not implemented yet")
    p_wire.add_argument("kernel_tree")
    p_wire.add_argument("susfs_patch")
    p_wire.set_defaults(func=cmd_wire)

    p_restage = sub.add_parser("restage", help="strip + fresh wire - cascade engine not implemented yet")
    p_restage.add_argument("kernel_tree")
    p_restage.add_argument("new_susfs_patch")
    p_restage.set_defaults(func=cmd_restage)

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
