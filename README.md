# susfs loom (v0.5)

STATUS : WORK IN PROGRESS 

Patch-engine for keeping susfs4ksu hooks working across kernel/susfs
version bumps. Detect + Strip + Cascade tiers 0-2 + Stage 3 Verify are
built. `loom wire` and `loom restage` handle exact matches, bounded 3-way
drift, and C function-anchor relocation, and can optionally confirm the
result afterward with `loom verify` (macro presence + syntax sanity).
Tier 3 (Coccinelle semantic patch) is still design-only on purpose — see
"what's actually here vs not" below for why that one's deferred rather
than half-built. Tier 4 (human handoff) ships as a `--handoff` text view.
Anything no tier resolves is reported as unresolved/conflict and left
untouched on disk, never guessed at or silently dropped.

**First real device-tree run (v0.5):** an already-hooked `android12-5.10`
tree, 118 hunks across 23 files — 72 resolved automatically (34 exact, 38
3-way), 3 files fully clean and safe to write. Spot-checking the
unresolved hunks by hand found a real bug, not a fundamental gap: a
cosmetic double-blank-line in the tree was making `git merge-file` report
a spurious conflict on an otherwise-clean hunk. Fixed in this version —
see §6/§7 of the design doc for the full writeup and the regression test
that reproduces it.

Strip is still useful standalone too: if you already have susfs hooked
into a tree and want to bump to a newer susfs release, strip reconstructs
the pre-hook tree without needing the old patch file, which half the time
nobody still has lying around after fixing a few .rej files by hand.

## install

```bash
pip install -e .
```

Needs `unifdef` and `git` on PATH (`apt install unifdef`; git's almost
certainly already there). `git merge-file` - used for the tier 1/2 3-way
merge - ships with git itself, no separate install, and doesn't require
the tree to actually be a git repo.

Install Tier 2's optional C parser with `pip install -e '.[tier2]'`.

## usage

```bash
# stage 0 - what tree am I even looking at
loom detect /path/to/kernel_tree

# stage 1 - dry run by default, shows what strip WOULD do
loom strip /path/to/kernel_tree /path/to/50_add_susfs_in_new.patch
loom strip /path/to/kernel_tree /path/to/new.patch --apply   # write it back
                                                              # (coverage-flagged files skipped unless --force)

# stage 2 - fresh wire: cascade tiers 0-2 against a clean-ish tree
loom wire /path/to/kernel_tree /path/to/susfs_patch
loom wire /path/to/kernel_tree /path/to/susfs_patch --apply  # write fully-resolved files back

# restage: strip, then hand the result to the same cascade engine
loom restage /path/to/kernel_tree /path/to/new_susfs_patch
loom restage /path/to/kernel_tree /path/to/new_susfs_patch --apply

# stage 3 - verify: confirm expected macros + syntax sanity post-apply
loom verify /path/to/kernel_tree /path/to/susfs_patch
loom verify /path/to/kernel_tree /path/to/susfs_patch --compile  # advisory cc -fsyntax-only

# or run it automatically right after wire/restage --apply:
loom wire /path/to/kernel_tree /path/to/susfs_patch --apply --verify
loom restage /path/to/kernel_tree /path/to/new_susfs_patch --apply --verify

# --json on any command for scripting/CI
# --handoff on wire/restage shows Tier 4's closest-target-region view for
#   whatever's still unresolved after tiers 0-2
```

`wire`/`restage` exit 1 if any hunk is left unresolved after tiers 0-2 (or,
with `--verify`, if verify finds a problem in what got written), so it's
CI-safe to gate on the exit code - a clean exit 0 means every touched hunk
actually resolved somewhere and, if you asked for it, that the result still
carries the macros it should and still parses as valid C.

## what's actually here vs not

**works:**
- kernel version / GKI branch guessing from Makefile + build.config
- susfs presence + macro set detection
- KernelSU fork ID (KernelSU-Next / SukiSU-Ultra / baseline, via file
  signatures - see KSU_FORK_SIGNATURES in detect.py)
- strip for both #ifdef blocks (unifdef) and Kbuild one-liners (regex)
- coverage check that catches leftover susfs tokens instead of pretending
  everything's fine
- cascade tier 0 (exact context match), tier 1 (bounded 3-way merge), and
  tier 2 (tree-sitter C-function anchor relocation followed by the same
  3-way merge)
- `loom wire` and `loom restage` run the above for real, per-hunk, and
  report which tier resolved each one - never a bare pass/fail
- stage 3 verify (`verify.py`) - for every file the patch touches, checks
  (a) every `CONFIG_KSU_SUSFS*` macro that patch's own hunks introduce in
  that file is actually present afterward, and (b) the file still parses
  as one well-formed C translation unit (`tree-sitter`'s own error
  recovery when the tier-2 extra is installed, a comment/string-aware
  brace-balance scan when it isn't - verify never hard-requires tier 2's
  dependency). `--compile` adds an opt-in, advisory-only `cc -fsyntax-only`
  pass; it never affects verify's pass/fail status, because a lone kernel
  `.c` file essentially never compiles standalone outside kbuild (missing
  generated headers, arch config, etc.) - useful as a second opinion when
  it happens to work, uninformative when it doesn't
- tier 4 human handoff (`--handoff` on `wire`/`restage`) - for every hunk
  still unresolved after tiers 0-2, shows the patch's own old-image beside
  either the real 3-way conflict output or the closest textual candidate
  region loom could find in the tree, so there's something concrete to
  start from instead of a bare `.rej`

**doesn't exist yet, on purpose:**
- tier 3 (semantic patches via Coccinelle) - deliberately deferred, not
  half-built: a `.cocci` semantic patch is a hand-authored artifact per
  hook point, and the design's own roadmap (item 4) is to write those for
  "whichever hook points empirically fail most often once tiers 0-2 are
  collecting real failure data" - that data doesn't exist yet from a
  single test fixture, so a "tier 3" built today would either be fake
  (a weaker heuristic wearing tier 3's name) or premature (patches
  authored against guessed-at hook points instead of measured ones).
  Hunks that would need it are reported as `unresolved`, not guessed at.
- structural fingerprinting / the community cache idea - there's a schema
  stub (`TreeFingerprint`) so it won't be a breaking change to add later,
  but nothing populates it yet

## tests

Ran against a real susfs4ksu patch pulled from `Simonpunks/susfs4ksu`'s
`gki-android14-6.1` branch, not something I made up - specifically the
`mnt_alloc_group_id` case with a goto label defined inside the guarded
branch, which is the gnarliest of the ifdef patterns susfs uses. The
cascade tests reuse the same real patch for exact-match and file-not-found
cases; the drift/conflict scenarios (tier 1 succeeding vs. a genuine
overlap) are synthetic on purpose - there's no single real fixture that
demonstrates both "line numbers shifted" and "vendor fought susfs for the
same insertion point" cleanly.

```bash
pip install pytest
pytest tests/ -v
```

`test_verify.py` uses small synthetic patch/tree pairs rather than the real
fixture, deliberately - verify reacts to whatever's left on disk afterward,
so a constructed before/after pair exercises every status (ok /
missing-macros / syntax-error / not-found) directly, including the
brace-balance fallback path when `tree-sitter-c` isn't installed.

## where this'll probably break

- if susfs ever stops prefixing macros with `CONFIG_KSU_SUSFS`, the
  extraction regex needs a one-line update (patchutils.py)
- if it starts guarding stuff some way that's neither #ifdef nor a Kbuild
  one-liner, strip won't know what to do with it - it'll just leave the
  tokens in and the coverage check will flag it, so at least it fails
  loud instead of writing garbage
- the KSU fork signatures are hand-maintained, not self-updating like the
  macro list is. new fork shows up, it'll just report "unknown" until
  someone adds a marker for it - annoying but safe
- tier 1's window search (`_locate_best_window` in cascade.py) is a local
  search bounded by `SEARCH_WINDOW` (400 lines) around the patch's
  recorded offset, not a whole-file search - if drift pushes a hook site
  further than that, it'll fall through to unresolved rather than risk
  matching the wrong location. Tier 2 can instead search the uniquely named
  C function when the patch hunk contains a usable function signature
