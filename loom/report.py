from __future__ import annotations

import dataclasses
import json
from typing import Any

from .cascade import CascadeReport
from .detect import DetectResult
from .strip import StripReport

STATUS_ICON = {
    "stripped": "✅",
    "unchanged": "➖",
    "coverage-flag": "⚠️ ",
    "not-found": "❔",
    "error": "❌",
}

HUNK_STATUS_ICON = {
    "applied": "✅",
    "conflict": "⚔️ ",
    "unresolved": "❔",
    "file-not-found": "❔",
}


def _asdict(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _asdict(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_asdict(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _asdict(v) for k, v in obj.items()}
    return obj


def detect_to_json(result: DetectResult) -> str:
    return json.dumps(_asdict(result), indent=2)


def detect_to_text(result: DetectResult) -> str:
    k, s, f = result.kernel, result.susfs, result.ksu_fork
    lines = [
        f"Tree:              {result.tree_path}",
        f"Kernel version:    {k.short}" + (f"  (EXTRAVERSION={k.extraversion})" if k.extraversion else ""),
        f"GKI branch guess:  {k.gki_branch_guess or 'not detected'}"
        + (f"  [{k.evidence}]" if k.gki_branch_guess else ""),
        "",
        f"susfs present:     {'yes' if s.present else 'no'}",
    ]
    if s.present:
        lines.append(f"susfs macros seen: {len(s.macros_found)}")
        for m in s.macros_found:
            lines.append(f"                     - {m}")
    lines.append("")
    if not f.present:
        lines.append("KernelSU:          not vendored in this tree")
    else:
        lines.append(f"KernelSU fork:     {f.fork or 'unrecognized'}  (confidence: {f.confidence})")
        for e in f.evidence:
            lines.append(f"                     evidence: {e}")
    if not result.fingerprint.implemented:
        lines.append("")
        lines.append("(structural fingerprint not computed yet, schema stub only)")
    return "\n".join(lines)


def strip_to_json(report: StripReport) -> str:
    d = {
        "patch": str(report.patch.path),
        "macros": report.patch.macros,
        "results": [
            {
                "path": r.path,
                "kind": r.kind,
                "status": r.status,
                "detail": r.detail,
                "surviving_tokens": r.surviving_tokens,
            }
            for r in report.results
        ],
    }
    return json.dumps(d, indent=2)


def strip_to_text(report: StripReport) -> str:
    lines = [
        f"Patch:   {report.patch.path}",
        f"Macros:  {', '.join(report.patch.macros)}",
        "",
        f"{'File':<40} {'Kind':<15} {'Status'}",
        "-" * 75,
    ]
    for r in report.results:
        icon = STATUS_ICON.get(r.status, "?")
        lines.append(f"{r.path:<40} {r.kind:<15} {icon} {r.status}" + (f"  ({r.detail})" if r.detail else ""))

    flagged = report.needs_review
    if flagged:
        lines.append("")
        lines.append(f"⚠️  {len(flagged)} file(s) need a human look before restage can go on:")
        for r in flagged:
            lines.append(f"  {r.path}: {r.detail}")
            for lineno, linetext in r.surviving_tokens[:5]:
                lines.append(f"    L{lineno}: {linetext}")
            if len(r.surviving_tokens) > 5:
                lines.append(f"    ... and {len(r.surviving_tokens) - 5} more")
    else:
        lines.append("")
        lines.append(f"✅ all {report.clean_count} touched files clean, good to hand off to fresh wire")
    return "\n".join(lines)


def cascade_to_json(report: CascadeReport) -> str:
    d = {
        "results": [
            {
                "file": r.file,
                "hunk_index": r.hunk_index,
                "section": r.section,
                "tier": r.tier,
                "status": r.status,
                "detail": r.detail,
                "conflict_text": r.conflict_text,
                "handoff_text": r.handoff_text,
                "handoff_start": r.handoff_start,
                "patch_old_text": r.patch_old_text,
            }
            for r in report.results
        ],
        "by_tier_count": report.by_tier_count,
        "resolved_files": sorted(report.resolved_file_text.keys()),
    }
    return json.dumps(d, indent=2)


def cascade_to_text(report: CascadeReport, include_handoff: bool = False) -> str:
    counts = report.by_tier_count
    lines = [
        f"{'File':<30} {'#':<4} {'Section':<28} {'Tier':<6} Status",
        "-" * 90,
    ]
    for r in report.results:
        icon = HUNK_STATUS_ICON.get(r.status, "?")
        tier = f"T{r.tier}" if r.tier is not None else "-"
        section = (r.section[:26] + "…") if len(r.section) > 27 else r.section
        lines.append(
            f"{r.file:<30} {r.hunk_index:<4} {section:<28} {tier:<6} {icon} {r.status}"
        )

    lines.append("")
    lines.append(
        f"tier 0 (exact): {counts['tier-0']}   "
        f"tier 1 (3-way): {counts['tier-1']}   "
        f"tier 2 (AST anchor): {counts['tier-2']}   "
        f"unresolved: {counts['unresolved']}"
    )

    unresolved = report.unresolved
    if unresolved:
        lines.append("")
        lines.append(
            f"❔ {len(unresolved)} hunk(s) not resolved by tiers 0-2 - need tier 3 "
            "(semantic patch) or a Tier 4 human decision:"
        )
        for r in unresolved:
            lines.append(f"  {r.file} [hunk {r.hunk_index}]: {r.detail}")
        if include_handoff:
            lines.append("")
            lines.append("Tier 4 handoff — patch context and closest target region:")
            for r in unresolved:
                lines.extend([
                    "",
                    f"--- {r.file} hunk {r.hunk_index} ({r.section or 'no section'})",
                    "patch old-image:",
                ])
                lines.append(r.patch_old_text or "<empty old-image>")
                if r.conflict_text:
                    lines.extend(["3-way conflict:", r.conflict_text or ""])
                else:
                    lines.append(f"target candidate (starting line {r.handoff_start}):")
                    lines.append(r.handoff_text or "<no candidate text>")
    else:
        lines.append("")
        lines.append(f"✅ every touched hunk resolved by tiers 0-2, {len(report.resolved_file_text)} file(s) ready to write")

    if report.resolved_file_text:
        lines.append("")
        lines.append(f"files fully resolved (safe to --apply): {', '.join(sorted(report.resolved_file_text))}")

    return "\n".join(lines)
