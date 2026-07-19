__version__ = "0.4.0"

# detect + strip + cascade tiers 0-2 (exact, 3-way merge, AST anchor) +
# stage 3 verify (macro-presence + syntax sanity post-apply). tier 3
# (coccinelle semantic patch) is still on paper - see design doc §5/§10,
# it's deliberately deferred until real cascade failure data exists to
# decide which hook points are worth hand-authoring one for. tier 4
# (human handoff) ships as the `--handoff` text view on wire/restage.
