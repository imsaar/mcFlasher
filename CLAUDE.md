# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file macOS-only Python TUI (`macflasher.py`) that flashes ISO/IMG images to USB drives, in the spirit of balenaEtcher. The whole program is one file plus a one-line `requirements.txt`. There is no build step, no test suite, no lint config — adding any of those should be deliberate, not reflexive.

`README.md` has the full user-facing docs; don't duplicate it here. Update it when behavior changes.

## Commands

```bash
# Install the only runtime dep
python3 -m pip install -r requirements.txt

# Run (auto re-execs under sudo if not root)
sudo python3 macflasher.py [path/to/image.iso]

# Quick syntax/parse check without executing
python3 -c "import ast; ast.parse(open('macflasher.py').read())"
```

End-to-end testing requires a physical USB drive plugged into the Mac. There is no mock harness; if you change the flash/verify path, say so explicitly rather than claiming it works.

## Architecture notes worth knowing before editing

- **Privilege escalation via `os.execvp("sudo", ...)`**: the process replaces itself, so any state set up before `ensure_root()` is discarded on the re-exec. Keep `ensure_root()` near the top of `main()`.
- **Raw vs. buffered device**: writes go to `/dev/rdiskN` (raw character device), not `/dev/diskN`. The `r` prefix is load-bearing — without it throughput drops 5–10× and progress reporting looks frozen because the kernel batches writes. Don't "simplify" by removing it.
- **`F_NOCACHE = 48`** is hardcoded because Python's `fcntl` module doesn't expose the macOS-specific constant. It's set on the verify-pass file descriptor so the read-back hits the media, not the page cache. If you remove it, the verify becomes meaningless.
- **Hash-during-write**: the source SHA-256 is computed in the same loop as the write, so flashing only reads the source file once. Verify then re-reads only the disk. Don't refactor into separate "compute source hash" / "flash" phases — that doubles read work on multi-GB ISOs.
- **Disk filter**: `diskutil list -plist external physical` is the safety mechanism — it excludes the internal SSD and virtual disks. Don't relax this filter.
- **Type-to-confirm**: the destructive prompt requires typing the disk identifier verbatim (e.g. `disk4`). A `[y/N]` prompt is not an acceptable substitute.
- **`from __future__ import annotations`** is at the top so the `str | None` / `list[dict]` annotations work on Python 3.9. Keep it if you add more annotations.

## Things that will look like bugs but aren't

- The `_open_nocache` helper swallows `OSError` from the `fcntl` call — this is intentional so the script still works on filesystems where `F_NOCACHE` isn't applicable, falling back to cached I/O.
- `subprocess.run(["diskutil", "eject", ...], check=False)` deliberately doesn't raise if eject fails — the flash already succeeded by then; an eject failure shouldn't make the exit code nonzero.
- `assert src_hash is not None` after a `flash(... hash_during_write=not args.no_verify)` call is correct: when `--no-verify` is set we never reach the assert, and when it isn't we always have a hash. If you change the flag plumbing, reconsider the assert.
