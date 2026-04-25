#!/usr/bin/env python3
"""macflasher — flash ISO/IMG images to USB drives on macOS.

Run:  sudo python3 macflasher.py [path/to/image.iso]
(re-execs under sudo if needed; raw disk access requires root)
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import (
        BarColumn,
        DownloadColumn,
        Progress,
        TextColumn,
        TimeRemainingColumn,
        TransferSpeedColumn,
    )
    from rich.prompt import Prompt
    from rich.table import Table
except ImportError:
    print("Missing dependency: rich", file=sys.stderr)
    print("Install with:  python3 -m pip install rich", file=sys.stderr)
    sys.exit(1)

# macOS fcntl: bypass the unified buffer cache so verify reads come from media,
# not from pages still hot from the write pass.
F_NOCACHE = 48
CHUNK = 4 * 1024 * 1024

console = Console()


def fmt_size(n: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def ensure_macos() -> None:
    if sys.platform != "darwin":
        console.print("[red]macflasher only supports macOS.[/red]")
        sys.exit(1)


def ensure_root() -> None:
    if os.geteuid() == 0:
        return
    console.print("[yellow]Root required for raw disk writes — re-running under sudo…[/yellow]")
    os.execvp("sudo", ["sudo", sys.executable, *sys.argv])


def list_external_disks() -> list[dict]:
    out = subprocess.run(
        ["diskutil", "list", "-plist", "external", "physical"],
        capture_output=True, check=True,
    ).stdout
    data = plistlib.loads(out)
    disks: list[dict] = []
    for entry in data.get("AllDisksAndPartitions", []):
        device_id = entry.get("DeviceIdentifier")
        if not device_id:
            continue
        info_out = subprocess.run(
            ["diskutil", "info", "-plist", device_id],
            capture_output=True, check=True,
        ).stdout
        info = plistlib.loads(info_out)
        disks.append({
            "id": device_id,
            "size": info.get("TotalSize", 0),
            "name": info.get("MediaName") or info.get("VolumeName") or "(unnamed)",
            "protocol": info.get("BusProtocol", "?"),
            "removable": info.get("RemovableMedia", False),
        })
    return disks


def pick_iso(arg: str | None) -> Path:
    if arg:
        p = Path(arg).expanduser().resolve()
        if not p.is_file():
            console.print(f"[red]Not a file: {p}[/red]")
            sys.exit(1)
        return p
    while True:
        ans = Prompt.ask("Path to .iso/.img").strip().strip("'\"")
        p = Path(ans).expanduser().resolve()
        if p.is_file():
            return p
        console.print(f"[red]File not found: {p}[/red]")


def pick_disk(disks: list[dict]) -> dict:
    table = Table(title="External / removable disks")
    table.add_column("#", style="cyan", justify="right")
    table.add_column("Device")
    table.add_column("Size", justify="right")
    table.add_column("Bus")
    table.add_column("Name")
    for i, d in enumerate(disks, 1):
        table.add_row(str(i), f"/dev/{d['id']}", fmt_size(d["size"]), d["protocol"], d["name"])
    console.print(table)
    while True:
        ans = Prompt.ask("Select disk number ('q' to abort)").strip().lower()
        if ans in ("q", "quit", "exit"):
            sys.exit(0)
        try:
            i = int(ans)
            if 1 <= i <= len(disks):
                return disks[i - 1]
        except ValueError:
            pass
        console.print("[red]Invalid selection.[/red]")


def confirm_destruction(disk: dict, iso: Path) -> None:
    panel = Panel(
        f"[bold red]This will ERASE all data on /dev/{disk['id']}[/bold red]\n"
        f"  size: {fmt_size(disk['size'])}\n"
        f"  name: {disk['name']}\n"
        f"  bus:  {disk['protocol']}\n\n"
        f"Replacing with:\n  {iso} ({fmt_size(iso.stat().st_size)})",
        title="DESTRUCTIVE",
        border_style="red",
    )
    console.print(panel)
    typed = Prompt.ask(f"Type [bold]{disk['id']}[/bold] to confirm")
    if typed.strip() != disk["id"]:
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(0)


def unmount_disk(disk_id: str) -> None:
    console.print(f"Unmounting /dev/{disk_id}…")
    subprocess.run(["diskutil", "unmountDisk", f"/dev/{disk_id}"], check=True)


def _open_nocache(path: str, mode: str):
    f = open(path, mode, buffering=0)
    try:
        fcntl.fcntl(f.fileno(), F_NOCACHE, 1)
    except OSError:
        pass
    return f


def flash(iso: Path, disk_id: str, hash_during_write: bool) -> str | None:
    raw = f"/dev/r{disk_id}"
    total = iso.stat().st_size
    h = hashlib.sha256() if hash_during_write else None

    columns = (
        TextColumn("[bold blue]Flashing "),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("flash", total=total)
        with open(iso, "rb") as src, _open_nocache(raw, "wb") as dst:
            while True:
                buf = src.read(CHUNK)
                if not buf:
                    break
                dst.write(buf)
                if h is not None:
                    h.update(buf)
                progress.update(task, advance=len(buf))
            os.fsync(dst.fileno())
    return h.hexdigest() if h is not None else None


def verify(iso: Path, disk_id: str, expected_hex: str) -> bool:
    raw = f"/dev/r{disk_id}"
    total = iso.stat().st_size
    h = hashlib.sha256()

    columns = (
        TextColumn("[bold green]Verifying"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    )
    with Progress(*columns, console=console) as progress:
        task = progress.add_task("verify", total=total)
        with _open_nocache(raw, "rb") as f:
            remaining = total
            while remaining > 0:
                buf = f.read(min(CHUNK, remaining))
                if not buf:
                    break
                h.update(buf)
                remaining -= len(buf)
                progress.update(task, advance=len(buf))
    return h.hexdigest() == expected_hex


def eject(disk_id: str) -> None:
    subprocess.run(["diskutil", "eject", f"/dev/{disk_id}"], check=False)


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="macflasher",
        description="Flash ISO/IMG images to USB drives on macOS (balenaEtcher-style TUI).",
    )
    parser.add_argument("iso", nargs="?", help="path to .iso or .img image")
    parser.add_argument("--no-verify", action="store_true", help="skip post-flash SHA-256 verification")
    parser.add_argument("--no-eject", action="store_true", help="leave the disk attached after flashing")
    args = parser.parse_args()

    ensure_macos()
    ensure_root()

    iso = pick_iso(args.iso)
    console.print(f"Image: [cyan]{iso}[/cyan] ({fmt_size(iso.stat().st_size)})")

    disks = list_external_disks()
    if not disks:
        console.print("[red]No external disks detected. Plug in a USB drive and try again.[/red]")
        return 1
    disk = pick_disk(disks)

    if iso.stat().st_size > disk["size"]:
        console.print(
            f"[red]Image ({fmt_size(iso.stat().st_size)}) is larger than the disk "
            f"({fmt_size(disk['size'])}).[/red]"
        )
        return 1

    confirm_destruction(disk, iso)
    unmount_disk(disk["id"])

    t0 = time.monotonic()
    src_hash = flash(iso, disk["id"], hash_during_write=not args.no_verify)
    elapsed = time.monotonic() - t0
    console.print(
        f"Flashed in {elapsed:.1f}s "
        f"({fmt_size(iso.stat().st_size / max(elapsed, 1e-6))}/s)"
    )

    if not args.no_verify:
        assert src_hash is not None
        if verify(iso, disk["id"], src_hash):
            console.print("[bold green]✓ Verification passed[/bold green]")
        else:
            console.print("[bold red]✗ Verification FAILED — disk does not match image[/bold red]")
            return 2

    if not args.no_eject:
        eject(disk["id"])

    console.print("[bold green]Done.[/bold green]")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted.[/yellow]")
        sys.exit(130)
