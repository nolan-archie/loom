# susfs loom (v0.1)

Patch-engine for keeping susfs4ksu hooks working across kernel/susfs
version bumps. Right now this only does detect + strip (see design doc
roadmap item 1) - the actual patch-applying cascade engine isn't built.

Strip is the useful part on its own though: if you already have susfs
hooked into a tree and want to bump to a newer susfs release, strip
reconstructs the pre-hook tree without needing the old patch file, which
half the time nobody still has lying around after fixing a few .rej files
by hand.

## install

```bash
pip install -e .
```

Needs `unifdef` on PATH (`apt install unifdef`, or grab it out of the
kernel's own `scripts/` dir, it's been in there forever).

## usage

```bash
# stage 0 - what tree am I even looking at
loom detect /path/to/kernel_tree

# stage 1 - dry run by default, shows what strip WOULD do
loom strip /path/to/kernel_tree /path/to/50_add_susfs_in_new.patch

# actually write it back. files that fail the coverage check get skipped
# unless you also pass --force
loom strip /path/to/kernel_tree /path/to/new.patch --apply

# --json on either command for scripting/CI
```

After a clean strip you're on your own for actually applying the new
patch (`patch -p1` or `git apply`) - that part isn't automated yet.

## what's actually here vs not

**works:**
- kernel version / GKI branch guessing from Makefile + build.config
- susfs presence + macro set detection
- KernelSU fork ID (KernelSU-Next / SukiSU-Ultra / baseline, via file
  signatures - see KSU_FORK_SIGNATURES in detect.py)
- strip for both #ifdef blocks (unifdef) and Kbuild one-liners (regex)
- coverage check that catches leftover susfs tokens instead of pretending
  everything's fine

**doesn't exist yet:**
- `loom wire`, the actual cascade-apply engine (tiers 0-4 from the design
  doc). the CLI subcommand exists but just tells you it's not built.
- `loom restage`'s second half - it'll run strip for you but then you're
  on your own for applying the new patch
- structural fingerprinting / the community cache idea - there's a schema
  stub (`TreeFingerprint`) so it won't be a breaking change to add later,
  but nothing populates it

## tests

Ran against a real susfs4ksu patch pulled from `ShirkNeko/susfs4ksu`'s
`gki-android14-6.1` branch, not something I made up - specifically the
`mnt_alloc_group_id` case with a goto label defined inside the guarded
branch, which is the gnarliest of the ifdef patterns susfs uses.

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
