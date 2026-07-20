__version__ = "0.5.0"

# detect + strip + cascade tiers 0-2 (exact, 3-way merge, AST anchor) +
# stage 3 verify (macro-presence + syntax sanity post-apply). tiers 0-2's
# merge step normalizes cosmetic blank-line-count differences at the merge
# seam before handing base/ours to `git merge-file` (see
# `_collapse_blank_runs` in cascade.py) - found via a real device-tree
# run, not guessed at. tier 3 (coccinelle semantic patch) is still on
# paper - see design doc §5/§10, deliberately deferred until real cascade
# failure data (like the blank-line case above) identifies which hook
# points are worth hand-authoring one for. tier 4 (human handoff) ships
# as the `--handoff` text view.
