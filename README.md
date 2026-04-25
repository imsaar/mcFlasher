# macflasher

A single-file Python TUI for flashing bootable Linux ISO/IMG images to USB drives on macOS. Like balenaEtcher, but without the Electron tax — it runs in your terminal, uses macOS's own `diskutil`, and writes directly to the raw character device for full bus speed.

```
sudo python3 macflasher.py ubuntu-24.04.iso
```

---

## Features

- **External-only disk discovery.** Internal SSDs and virtual disks are filtered out by `diskutil list external physical`, so you can't accidentally overwrite your boot drive.
- **Type-to-confirm.** You must type the target's identifier (e.g. `disk4`) before any data is written.
- **Fast writes.** Writes go to `/dev/rdiskN` (raw character device) in 4 MiB chunks — typically 5–10× faster than the buffered `/dev/diskN` path.
- **Streaming SHA-256 verify.** The source hash is computed during the write pass; the verify pass re-reads the disk with `F_NOCACHE` set so the comparison hits the actual media, not the page cache.
- **Live progress.** Throughput, ETA, and percentage via [`rich`](https://github.com/Textualize/rich) progress bars for both flash and verify phases.
- **Auto-sudo.** Re-execs itself under `sudo` if invoked without root, since raw disk writes require it.
- **Auto-eject.** Disk is ejected on success so you can pull it out immediately.

## Requirements

- macOS (tested on Sonoma and Sequoia; should work on anything modern with `diskutil`)
- Python 3.9 or newer (any recent macOS ships this; or use Homebrew's `python3`)
- The [`rich`](https://pypi.org/project/rich/) library
- `sudo` rights on your account (for raw disk access)

## Installation

```bash
git clone <this-repo> macflasher
cd macflasher
python3 -m pip install -r requirements.txt
```

That's it — there's no build step. The whole tool is one file (`macflasher.py`).

If you'd rather not pollute your system Python, use a venv:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Then invoke with `sudo .venv/bin/python macflasher.py …` so sudo uses the venv's interpreter.

## Usage

```
macflasher.py [-h] [--no-verify] [--no-eject] [iso]
```

| Argument        | Meaning                                                   |
| --------------- | --------------------------------------------------------- |
| `iso`           | Path to the `.iso` or `.img` to flash. Prompts if omitted. |
| `--no-verify`   | Skip the post-flash SHA-256 read-back. Roughly halves total time. |
| `--no-eject`    | Don't `diskutil eject` when finished.                      |
| `-h`, `--help`  | Show usage and exit.                                       |

### Examples

Flash an Ubuntu ISO, prompted to pick a disk:

```bash
sudo python3 macflasher.py ~/Downloads/ubuntu-24.04.2-desktop-amd64.iso
```

Flash an Arch ISO and skip verification (faster, but trust your USB):

```bash
sudo python3 macflasher.py --no-verify ~/Downloads/archlinux.iso
```

Run with no arguments — the tool will prompt for the image path:

```bash
sudo python3 macflasher.py
```

### What you'll see

```
Image: /Users/you/Downloads/ubuntu-24.04.iso (5.7 GiB)
                External / removable disks
┏━━━┳━━━━━━━━━━━┳━━━━━━━━━━┳━━━━━━┳━━━━━━━━━━━━━━━━┓
┃ # ┃ Device    ┃    Size  ┃ Bus  ┃ Name           ┃
┡━━━╇━━━━━━━━━━━╇━━━━━━━━━━╇━━━━━━╇━━━━━━━━━━━━━━━━┩
│ 1 │ /dev/disk4│ 30.0 GiB │ USB  │ SanDisk Cruzer │
└───┴───────────┴──────────┴──────┴────────────────┘
Select disk number ('q' to abort): 1

╭───────────── DESTRUCTIVE ─────────────╮
│ This will ERASE all data on /dev/disk4│
│   size: 30.0 GiB                      │
│   name: SanDisk Cruzer                │
│   bus:  USB                           │
│                                       │
│ Replacing with:                       │
│   /Users/you/.../ubuntu-24.04.iso     │
│   (5.7 GiB)                           │
╰───────────────────────────────────────╯
Type disk4 to confirm: disk4

Unmounting /dev/disk4…
Flashing  ━━━━━━━━━━━━━━━━━━━━ 5.7/5.7 GiB 142 MB/s 0:00:00
Flashed in 41.2s (140.9 MiB/s)
Verifying ━━━━━━━━━━━━━━━━━━━━ 5.7/5.7 GiB 175 MB/s 0:00:00
✓ Verification passed
Done.
```

## How it works

The whole thing is ~250 lines. The interesting parts:

1. **Disk enumeration** — `diskutil list -plist external physical` returns the set of plugged-in physical disks, filtered to *external* (so internal NVMe is excluded) and *physical* (so disk-images and APFS containers are excluded). For each whole disk, `diskutil info -plist diskN` gets size, bus, and media name. Output is parsed with the stdlib `plistlib`.

2. **Unmount** — Before writing, `diskutil unmountDisk /dev/diskN` releases all volumes on the disk so macOS doesn't fight us for the device. (Note: `unmountDisk`, not `eject` — eject also detaches the device, which we don't want until we're finished.)

3. **Raw write** — On macOS, `/dev/diskN` is a buffered block device while `/dev/rdiskN` is the unbuffered character device. Writes to the raw device go straight to the disk driver in big requests (we use 4 MiB) instead of being broken into 4 KiB pages by the kernel buffer cache. Empirically this is 5–10× faster on USB 3 flash drives.

4. **Hash during write** — A `hashlib.sha256` is fed every chunk on the way past, so we get the source digest "for free" during the flash pass. No second read of the source file is needed.

5. **Verify with `F_NOCACHE`** — After `fsync`, the read-back pass opens `/dev/rdiskN` and sets `fcntl(F_NOCACHE)` (macOS-specific flag, value `48`) on the file descriptor. This bypasses the unified buffer cache so reads pull bytes off the actual media — important, because otherwise a "verify" might just be reading back the same pages we just wrote.

6. **Auto-sudo** — If `os.geteuid() != 0`, the script `os.execvp`s itself under `sudo` with the same argv. You enter your password once and the same process continues. This is friendlier than aborting with "please run with sudo".

## Safety notes

- The tool only ever touches disks reported as **external + physical** by `diskutil`. Your internal SSD will not appear in the picker.
- Confirmation requires typing the exact disk identifier (e.g. `disk4`). A bare `y` won't do it.
- The read-back verify is on by default. If it fails, the exit code is `2` and you'll see a red banner — don't trust the resulting USB; reflash or swap the drive.
- There is no undo. Everything on the chosen disk is gone the moment the flash starts.

## Troubleshooting

**"Resource busy" or `unmountDisk` fails.**
Some macOS background process (Spotlight, Time Machine, Photos) may have grabbed the disk. Eject any mounted volumes from Finder, wait a few seconds, then re-run. As a last resort, `diskutil unmountDisk force /dev/diskN` will work, but it's a sledgehammer.

**Verify fails on a known-good ISO.**
Your USB drive is probably failing — cheap or counterfeit flash sometimes reports more capacity than it has. Try a different drive. (If verify fails repeatedly on multiple drives with one specific ISO, the ISO download is corrupt; re-download and check the upstream SHA-256.)

**"No external disks detected."**
The drive isn't enumerating. Try a different USB port (front-panel hubs on some Macs are flaky), or a different cable for portable SSDs. Confirm with `diskutil list external physical` directly.

**Throughput is much lower than expected.**
USB 2.0 ports cap around 35 MB/s; USB 3 should do 100+ MB/s on decent flash. If you're plugged into a USB-C → USB-A adapter on a hub, you may have silently fallen back to USB 2. Try plugging directly into the Mac.

**The script hangs at "Flashing" with no progress movement.**
You're probably writing to `/dev/diskN` instead of `/dev/rdiskN` somehow (e.g. you edited the script). The buffered device path will appear hung for 30+ seconds at a time as the kernel batches writes. Use the raw device.

## Limitations / non-goals

- **macOS only.** Linux/Windows aren't supported — `diskutil` and `F_NOCACHE` are Apple-specific. On Linux you'd use `lsblk` and `O_DIRECT`; on Windows it's a different universe entirely.
- **No compressed images.** `.iso.gz`, `.iso.xz`, `.zip` aren't decompressed on the fly. Decompress first (`gunzip`, `xz -d`, `unzip`) and feed the resulting `.iso` or `.img`.
- **No `.dmg`.** Apple disk images aren't raw byte streams in general; use `hdiutil convert` to turn them into `.img` first if you really need to.
- **No GUI.** Use balenaEtcher proper if you want a window with buttons.
- **No multi-target.** One image, one disk, one run. (Trivially scriptable in a shell loop if you need several.)

## License

MIT. Take it, fork it, ship it.
