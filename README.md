# susfs loom (v0.3)

Patch-engine for keeping susfs4ksu hooks working across kernel/susfs
version bumps. Detect + Strip + Cascade tiers 0-2 are built. `loom wire`
and `loom restage` now handle exact matches, bounded 3-way drift, and C
function-anchor relocation. Tiers 3-4 (semantic patch and human handoff)
are still design-only, so anything they would be needed for gets reported
as unresolved, never guessed at or silently dropped.

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

# --json on any command for scripting/CI
```

`wire`/`restage` exit 1 if any hunk is left unresolved after tiers 0-2,
so it's CI-safe to gate on the exit code - a clean exit 0 means every
touched hunk actually resolved somewhere, not that some subset got
silently skipped.

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

**doesn't exist yet:**
- tier 3 (semantic patches via Coccinelle) and tier 4 (human handoff view)
  - hunks that need either are reported as `unresolved`/`conflict` and left
  untouched on disk
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
