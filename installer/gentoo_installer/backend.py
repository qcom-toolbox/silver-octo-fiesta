"""The installation engine.

No Qt in here: the GUI drives it from a worker thread and the tests drive it
with a recording runner. Every change to the machine goes through Runner, so
--dry-run prints what would happen without touching anything.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from . import system

TARGET = "/mnt/gentoo-target"
SOURCE_MOUNT = "/mnt/gentoo-source"
LIVE_FILES = "/usr/share/gentoo-desktop/live-files.list"
# Pristine lower layer of the live root when booted with rd.live.overlay.overlayfs.
LIVE_ROOT_BASE = "/run/rootfsbase"
SQUASHFS_CANDIDATES = (
    "/run/initramfs/live/LiveOS/squashfs.img",
    "/run/initramfs/squashed.img",
)
MIN_DISK_BYTES = 30 * system.GIB
# grub-mkconfig only writes root=UUID=... when udev made this link.
DEV_BY_UUID = "/dev/disk/by-uuid"

USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
HOSTNAME_RE = re.compile(r"^(?!-)[A-Za-z0-9-]{1,63}(?<!-)$")
RESERVED_USERNAMES = {
    "root", "live", "bin", "daemon", "adm", "lp", "sync", "shutdown", "halt", "mail",
    "news", "uucp", "operator", "portage", "nobody", "sddm", "polkitd", "messagebus",
    "man", "sshd", "games", "ftp", "pipewire", "avahi", "cron",
}
USER_GROUPS = (
    "users", "wheel", "audio", "video", "input", "render", "plugdev", "usb", "lp", "pipewire",
    # virtual machines: QEMU/KVM, virt-manager (libvirt), VirtualBox
    "kvm", "libvirt", "vboxusers",
)
BIOS_BOOT_GUID = "21686148-6449-6E6F-744E-656564454649"
BTRFS_OPTS = "compress=zstd:1,noatime"
# Btrfs subvolumes and where they are mounted. Logs and caches live outside
# "@" so snapshots of the system stay small and a rollback keeps the logs.
BTRFS_SUBVOLUMES = (
    ("@", "/"),
    ("@home", "/home"),
    ("@snapshots", "/.snapshots"),
    ("@log", "/var/log"),
    ("@cache", "/var/cache"),
)
LUKS_NAME = "cryptroot"
ZFS_POOL = "rpool"
ZFS_ROOT = f"{ZFS_POOL}/ROOT/gentoo"
# (dataset, extra `zfs create` options). Like the Btrfs layout: logs and
# caches are separate so system snapshots stay small.
ZFS_DATASETS = (
    (f"{ZFS_POOL}/ROOT", ["-o", "canmount=off", "-o", "mountpoint=none"]),
    (ZFS_ROOT, ["-o", "canmount=noauto", "-o", "mountpoint=/"]),
    (f"{ZFS_POOL}/home", ["-o", "mountpoint=/home"]),
    (f"{ZFS_POOL}/var", ["-o", "canmount=off", "-o", "mountpoint=/var"]),
    (f"{ZFS_POOL}/var/log", []),
    (f"{ZFS_POOL}/var/cache", []),
)
ZFS_POOL_OPTIONS = [
    "-o", "ashift=12", "-o", "autotrim=on",
    "-O", "acltype=posixacl", "-O", "xattr=sa", "-O", "relatime=on", "-O", "compression=zstd",
    "-O", "dnodesize=auto", "-O", "normalization=formD", "-O", "canmount=off", "-O", "mountpoint=none",
]
FILESYSTEMS = ("btrfs", "ext4", "zfs")

SNAPPER_CONFIG = """\
# Snapper configuration for / (written by the installer).
# Snapshots are taken by /etc/cron.hourly/gentoo-snapshot and before/after
# every gentoo-update; old ones are removed by /etc/cron.daily/gentoo-snapshot.
SUBVOLUME="/"
FSTYPE="btrfs"
QGROUP=""
SPACE_LIMIT="0.5"
FREE_LIMIT="0.2"
ALLOW_USERS=""
ALLOW_GROUPS="wheel"
SYNC_ACL="yes"
BACKGROUND_COMPARISON="yes"
NUMBER_CLEANUP="yes"
NUMBER_MIN_AGE="1800"
NUMBER_LIMIT="10"
NUMBER_LIMIT_IMPORTANT="5"
TIMELINE_CREATE="yes"
TIMELINE_CLEANUP="yes"
TIMELINE_MIN_AGE="1800"
TIMELINE_LIMIT_HOURLY="10"
TIMELINE_LIMIT_DAILY="7"
TIMELINE_LIMIT_WEEKLY="4"
TIMELINE_LIMIT_MONTHLY="3"
TIMELINE_LIMIT_YEARLY="0"
EMPTY_PRE_POST_CLEANUP="yes"
EMPTY_PRE_POST_MIN_AGE="1800"
"""
MIN_PASSPHRASE = 8

RSYNC_EXCLUDES = (
    "/proc/*", "/sys/*", "/dev/*", "/run/*", "/tmp/*", "/mnt/*", "/media/*",
    "/var/tmp/*", "/lost+found", "/efi/*", "/swapfile",
)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_username(name: str) -> str | None:
    if not name:
        return "Please choose a user name."
    if not USERNAME_RE.match(name):
        return ("The user name must start with a lowercase letter and may only contain "
                "lowercase letters, digits, '-' and '_' (max. 32 characters).")
    if name in RESERVED_USERNAMES:
        return f"'{name}' is reserved by the system, please choose another user name."
    return None


def validate_hostname(name: str) -> str | None:
    if not name:
        return "Please choose a computer name."
    if not HOSTNAME_RE.match(name):
        return ("The computer name may only contain letters, digits and '-', "
                "and must not start or end with '-' (max. 63 characters).")
    return None


def validate_password(password: str, confirm: str) -> str | None:
    if not password:
        return "Please choose a password."
    if password != confirm:
        return "The passwords do not match."
    if ":" in password or "\n" in password:
        return "The password must not contain ':' or line breaks."
    return None


def validate_passphrase(passphrase: str, confirm: str) -> str | None:
    if len(passphrase) < MIN_PASSPHRASE:
        return f"The encryption passphrase must have at least {MIN_PASSPHRASE} characters."
    if passphrase != confirm:
        return "The encryption passphrases do not match."
    return None


def suggest_username(full_name: str) -> str:
    first = (full_name.strip().split() or [""])[0].lower()
    name = re.sub(r"[^a-z0-9_-]", "", first)
    if name and not name[0].isalpha():
        name = "u" + name
    return name[:32]


def partition_path(disk: str, number: int) -> str:
    """/dev/sda + 1 -> /dev/sda1, /dev/nvme0n1 + 1 -> /dev/nvme0n1p1."""
    return f"{disk}p{number}" if disk[-1:].isdigit() else f"{disk}{number}"


_PROGRESS_RE = re.compile(r"\s(\d{1,3})%\s")


def parse_rsync_progress(line: str) -> int | None:
    """Percentage from an `rsync --info=progress2` line."""
    match = _PROGRESS_RE.search(f" {line} ")
    return int(match.group(1)) if match else None


# ---------------------------------------------------------------------------
# Configuration chosen in the GUI
# ---------------------------------------------------------------------------
@dataclass
class InstallConfig:
    locale: str = "en_US.UTF-8"
    timezone: str = "UTC"
    keyboard_layout: str = "us"
    keyboard_variant: str = ""
    # "erase": wipe `disk` and partition it automatically.
    # "manual": use existing `root_partition` (+ `efi_partition`).
    mode: str = "erase"
    disk: str = ""
    root_partition: str = ""
    efi_partition: str = ""
    format_efi: bool = False
    filesystem: str = "btrfs"
    # Encryption of the system (erase mode only): LUKS2 for Btrfs/ext4,
    # native ZFS encryption for ZFS.
    encrypt: bool = False
    luks_passphrase: str = ""
    # Automatic snapshots: Snapper on Btrfs, zfs snapshots on ZFS.
    snapshots: bool = True
    full_name: str = ""
    username: str = ""
    password: str = ""
    hostname: str = "gentoo-pc"
    root_password_same: bool = False
    autologin: bool = False
    uefi: bool = True

    def summary(self) -> list[tuple[str, str]]:
        if self.mode == "erase":
            storage = f"Erase {self.disk} completely and install there"
        else:
            efi = f", EFI: {self.efi_partition}" if self.efi_partition else ""
            fmt = " (formatted)" if self.format_efi else " (kept, dual boot friendly)"
            storage = f"Root: {self.root_partition} (formatted){efi}{fmt if efi else ''}"
        return [
            ("Language", self.locale),
            ("Time zone", self.timezone),
            ("Keyboard", self.keyboard_layout + (f" ({self.keyboard_variant})" if self.keyboard_variant else "")),
            ("Installation", storage),
            ("File system", {
                "btrfs": "Btrfs (zstd compression; @, @home, @snapshots, @log, @cache subvolumes)",
                "zfs": f"ZFS (pool {ZFS_POOL}, zstd compression; ROOT/gentoo, home, var/log, var/cache)",
            }.get(self.filesystem, "ext4")),
            ("Encryption", ("Native ZFS encryption" if self.filesystem == "zfs" else "LUKS2")
             + ", passphrase asked at every boot (/boot stays unencrypted)" if self.encrypt else "None"),
            ("Snapshots", self.snapshot_backend_description()),
            ("Boot mode", "UEFI" if self.uefi else "Legacy BIOS"),
            ("User", f"{self.full_name} ({self.username})" if self.full_name else self.username),
            ("Computer name", self.hostname),
            ("Administrator", "Same password as the user" if self.root_password_same else "Locked, use sudo"),
            ("Automatic login", "Yes" if self.autologin else "No"),
        ]


    def snapshot_backend(self) -> str:
        """Value of SNAPSHOTS in /etc/conf.d/gentoo-snapshots."""
        if not self.snapshots:
            return "none"
        return {"btrfs": "snapper", "zfs": "zfs"}.get(self.filesystem, "none")

    def snapshot_backend_description(self) -> str:
        return {
            "snapper": "Snapper: hourly/daily and before every gentoo-update",
            "zfs": "ZFS snapshots: hourly/daily and before every gentoo-update",
        }.get(self.snapshot_backend(), "None")


class InstallError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Command runner
# ---------------------------------------------------------------------------
class Runner:
    """Runs commands and file operations, or only logs them in dry-run mode."""

    def __init__(self, dry_run: bool = False, log: Callable[[str], None] = print):
        self.dry_run = dry_run
        self.log = log

    def run(
        self,
        cmd: Sequence[str],
        *,
        input: str | None = None,  # noqa: A002 - mirrors subprocess
        check: bool = True,
        capture: bool = False,
        on_output: Callable[[str], None] | None = None,
    ) -> str:
        self.log("$ " + shlex.join(cmd))
        if self.dry_run:
            return ""
        if on_output is not None:
            return self._stream(cmd, check, on_output)
        proc = subprocess.run(list(cmd), input=input, text=True, capture_output=True, check=False)
        if not capture and proc.stdout.strip():
            self.log(proc.stdout.rstrip())
        if proc.stderr.strip():
            self.log(proc.stderr.rstrip())
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip().splitlines()[-3:]
            raise InstallError(f"'{shlex.join(cmd)}' failed (exit code {proc.returncode}).\n" + "\n".join(detail))
        return proc.stdout

    def succeeds(self, cmd: Sequence[str]) -> bool:
        """Run a command, report whether it worked instead of raising."""
        try:
            self.run(cmd)
            return True
        except InstallError:
            return False

    def _stream(self, cmd: Sequence[str], check: bool, on_output: Callable[[str], None]) -> str:
        proc = subprocess.Popen(list(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        assert proc.stdout is not None
        buf = b""
        tail: list[str] = []
        while chunk := proc.stdout.read1(4096):
            buf += chunk
            parts = re.split(rb"[\r\n]", buf)
            buf = parts.pop()
            for part in parts:
                line = part.decode(errors="replace").strip()
                if line:
                    on_output(line)
                    tail = (tail + [line])[-5:]
        if buf.strip():
            on_output(buf.decode(errors="replace").strip())
        code = proc.wait()
        if check and code != 0:
            raise InstallError(f"'{shlex.join(cmd)}' failed (exit code {code}).\n" + "\n".join(tail))
        return ""

    # File operations -----------------------------------------------------
    def write_file(self, path: str, content: str, mode: int = 0o644) -> None:
        self.log(f"write {path}")
        if self.dry_run:
            for line in content.rstrip("\n").splitlines():
                self.log(f"    | {line}")
            return
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(content)
        os.chmod(path, mode)

    def remove(self, path: str) -> None:
        self.log(f"remove {path}")
        if self.dry_run:
            return
        p = Path(path)
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p, ignore_errors=True)
        elif p.exists() or p.is_symlink():
            p.unlink()

    def symlink(self, target: str, link: str) -> None:
        self.log(f"symlink {link} -> {target}")
        if self.dry_run:
            return
        p = Path(link)
        if p.exists() or p.is_symlink():
            p.unlink()
        p.symlink_to(target)

    def mkdir(self, path: str) -> None:
        self.log(f"mkdir {path}")
        if not self.dry_run:
            Path(path).mkdir(parents=True, exist_ok=True)

    def read_file(self, path: str) -> str | None:
        try:
            return Path(path).read_text()
        except OSError:
            return None


# ---------------------------------------------------------------------------
# Installer
# ---------------------------------------------------------------------------
ProgressCallback = Callable[[float, str], None]


class Installer:
    # (method, weight in % of the progress bar, status text)
    STEPS = (
        ("prepare_disk", 3, "Partitioning the disk"),
        ("format", 3, "Creating file systems"),
        ("mount", 1, "Mounting file systems"),
        ("copy_system", 75, "Copying the system (this takes a few minutes)"),
        ("configure", 12, "Configuring the new system"),
        ("bootloader", 5, "Installing the boot loader"),
        ("finish", 1, "Finishing"),
    )

    def __init__(
        self,
        config: InstallConfig,
        runner: Runner,
        progress: ProgressCallback | None = None,
        edition: dict[str, str] | None = None,
        target: str = TARGET,
        live_files: str = LIVE_FILES,
        source: str | None = None,
    ):
        self.cfg = config
        self.r = runner
        self.progress = progress or (lambda fraction, text: None)
        self.edition = edition or system.read_edition()
        self.target = target
        self.live_files = live_files
        self.root_dev = ""  # device holding the root file system (the LUKS mapping if encrypted)
        self.efi_dev = ""
        self.boot_dev = ""  # separate /boot partition (only with encryption)
        self.luks_dev = ""  # the encrypted partition
        self.zfs_dev = ""  # the partition holding the ZFS pool
        self._luks_open = False
        self._zpool_created = False
        self._target_private = False
        # System to copy; found automatically on the live ISO (see _find_source).
        self.source = source or ""
        self._mounted_source = False
        self._chroot_mounts: list[str] = []
        self._done = 0.0

    # helpers -------------------------------------------------------------
    def t(self, path: str) -> str:
        """Path inside the target system."""
        rel = path.lstrip("/")
        return os.path.join(self.target, rel) if rel else self.target

    def chroot(self, *cmd: str, **kwargs) -> str:
        return self.r.run(["chroot", self.target, *cmd], **kwargs)

    def _report(self, step_fraction: float, weight: float, text: str) -> None:
        self.progress(min(1.0, (self._done + weight * step_fraction) / 100.0), text)

    # main ----------------------------------------------------------------
    def run(self) -> None:
        self._check_config()
        try:
            for name, weight, text in self.STEPS:
                self.r.log(f"=== {text}")
                self._report(0.0, weight, text)
                self._current_weight, self._current_text = weight, text
                getattr(self, name)()
                self._done += weight
                self._report(0.0, 0, text)
            self.progress(1.0, "Installation complete")
        finally:
            self.cleanup()

    def _check_config(self) -> None:
        c = self.cfg
        for err in (validate_username(c.username), validate_hostname(c.hostname),
                    validate_password(c.password, c.password)):
            if err:
                raise InstallError(err)
        if c.filesystem not in FILESYSTEMS:
            raise InstallError(f"Unsupported file system {c.filesystem}")
        if c.filesystem == "zfs" and c.mode != "erase":
            raise InstallError("ZFS is only available when erasing a whole disk.")
        if c.encrypt:
            if c.mode != "erase":
                raise InstallError("Encryption is only available when erasing a whole disk.")
            err = validate_passphrase(c.luks_passphrase, c.luks_passphrase)
            if err:
                raise InstallError(err)
        if c.mode == "erase":
            if not c.disk:
                raise InstallError("No disk selected.")
        elif c.mode == "manual":
            if not c.root_partition:
                raise InstallError("No root partition selected.")
            if c.uefi and not c.efi_partition:
                raise InstallError("UEFI systems need an EFI system partition.")
            if c.root_partition == c.efi_partition:
                raise InstallError("The root and EFI partitions must be different.")
            if not c.uefi and not c.disk:
                raise InstallError("Legacy BIOS installs need the disk for the boot loader.")
        else:
            raise InstallError(f"Unknown partitioning mode {c.mode}")

    # steps ---------------------------------------------------------------
    def prepare_disk(self) -> None:
        c = self.cfg
        if c.mode == "manual":
            self.root_dev = c.root_partition
            self.efi_dev = c.efi_partition if c.uefi else ""
            for dev in filter(None, (self.root_dev, self.efi_dev)):
                self.r.run(["umount", dev], check=False)
            return

        # Nothing on the disk may stay mounted or in use as swap.
        out = self.r.run(["lsblk", "--list", "--noheadings", "--paths", "--output", "NAME", c.disk],
                         check=False, capture=True)
        for dev in reversed(out.split()):
            self.r.run(["swapoff", dev], check=False)
            self.r.run(["umount", "--recursive", dev], check=False)

        self.r.run(["wipefs", "--all", "--force", c.disk])
        parts = ['size=1GiB, type=U, name="EFI system"' if c.uefi
                 else f'size=1MiB, type={BIOS_BOOT_GUID}, name="BIOS boot"']
        if self._separate_boot:
            # GRUB reads kernel and initramfs from a plain ext4 /boot. It never
            # has to open LUKS2 (Argon2id) or a ZFS pool with newer features.
            parts.append('size=1GiB, type=L, name="boot"')
        parts.append(f'type=L, name="{self.edition["DISTRO_SHORT"]}"')
        layout = "label: gpt\n" + "\n".join(parts) + "\n"
        self.r.run(["sfdisk", "--wipe", "always", "--wipe-partitions", "always", c.disk], input=layout)
        self.r.run(["partprobe", c.disk], check=False)
        self.r.run(["udevadm", "settle"], check=False)
        self.efi_dev = partition_path(c.disk, 1) if c.uefi else ""
        if self._separate_boot:
            self.boot_dev = partition_path(c.disk, 2)
            system_part = partition_path(c.disk, 3)
        else:
            system_part = partition_path(c.disk, 2)
        if c.filesystem == "zfs":
            self.zfs_dev = system_part
        elif c.encrypt:
            self.luks_dev = system_part
        else:
            self.root_dev = system_part

    @property
    def _separate_boot(self) -> bool:
        return self.cfg.encrypt or self.cfg.filesystem == "zfs"

    def _private_target(self) -> None:
        """Make the target directory a private mount point.

        "/" is usually a shared mount (systemd). Everything mounted below the
        target would otherwise also appear in other mount namespaces (sandboxed
        services); those copies cannot always be unmounted again, which keeps
        the new file systems busy: the LUKS volume could not be closed or the
        ZFS pool exported at the end.
        """
        if self._target_private:
            return
        self.r.mkdir(self.target)
        self.r.run(["mount", "--bind", self.target, self.target])
        self.r.run(["mount", "--make-private", self.target])
        self._target_private = True

    def format(self) -> None:
        self._private_target()
        c = self.cfg
        label = self.edition["DISTRO_SHORT"][:16]
        if self.efi_dev and (c.mode == "erase" or c.format_efi):
            self.r.run(["mkfs.vfat", "-F", "32", "-n", "EFI", self.efi_dev])
        if self.boot_dev:
            self.r.run(["mkfs.ext4", "-F", "-L", "boot", self.boot_dev])
        if c.filesystem == "zfs":
            self._create_zpool()
            return
        if c.encrypt:
            self.r.run(["cryptsetup", "luksFormat", "--type", "luks2", "--batch-mode",
                        "--key-file=-", self.luks_dev], input=c.luks_passphrase)
            self.r.run(["cryptsetup", "open", "--key-file=-", self.luks_dev, LUKS_NAME],
                       input=c.luks_passphrase)
            self._luks_open = True
            self.root_dev = f"/dev/mapper/{LUKS_NAME}"
        if c.filesystem == "btrfs":
            self.r.run(["mkfs.btrfs", "--force", "--label", label, self.root_dev])
        else:
            self.r.run(["mkfs.ext4", "-F", "-L", label, self.root_dev])
        self.r.run(["udevadm", "settle"], check=False)  # let udev see the new file systems

    def _create_zpool(self) -> None:
        c = self.cfg
        # The pool remembers the host id that created it; the installed
        # system must use the same one (copied in _setup_zfs) or the
        # initramfs refuses to import the pool.
        self.r.run(["zgenhostid", "-f"])
        self.r.run(["modprobe", "zfs"])
        cmd = ["zpool", "create", "-f", *ZFS_POOL_OPTIONS,
               "-o", "cachefile=/etc/zfs/zpool.cache", "-R", self.target]
        passphrase = None
        if c.encrypt:
            cmd += ["-O", "encryption=on", "-O", "keyformat=passphrase", "-O", "keylocation=prompt"]
            passphrase = c.luks_passphrase + "\n"
        cmd += [ZFS_POOL, self.zfs_dev]
        self.r.run(cmd, input=passphrase)
        self._zpool_created = True
        self.root_dev = ZFS_ROOT
        for dataset, options in ZFS_DATASETS:
            self.r.run(["zfs", "create", *options, dataset])
            if dataset == ZFS_ROOT:
                # canmount=noauto: mount / by hand before creating datasets below it
                self.r.run(["zfs", "mount", ZFS_ROOT])
        self.r.run(["zpool", "set", f"bootfs={ZFS_ROOT}", ZFS_POOL])

    def mount(self) -> None:
        self.r.mkdir(self.target)
        if self.cfg.filesystem == "zfs":
            pass  # the datasets are mounted below the pool's altroot already
        elif self.cfg.filesystem == "btrfs":
            self.r.run(["mount", self.root_dev, self.target])
            for sub, _ in BTRFS_SUBVOLUMES:
                self.r.run(["btrfs", "subvolume", "create", f"{self.target}/{sub}"])
            self.r.run(["umount", self.target])
            for sub, mountpoint in BTRFS_SUBVOLUMES:
                if mountpoint != "/":
                    self.r.mkdir(self.t(mountpoint))
                self.r.run(["mount", "-o", f"subvol={sub},{BTRFS_OPTS}", self.root_dev, self.t(mountpoint)])
        else:
            self.r.run(["mount", "-o", "noatime", self.root_dev, self.target])
        if self.boot_dev:
            self.r.mkdir(self.t("/boot"))
            self.r.run(["mount", "-o", "noatime", self.boot_dev, self.t("/boot")])
        if self.efi_dev:
            self.r.mkdir(self.t("/efi"))
            self.r.run(["mount", self.efi_dev, self.t("/efi")])

    def _find_source(self) -> str:
        if os.path.isdir(LIVE_ROOT_BASE) and os.listdir(LIVE_ROOT_BASE):
            return LIVE_ROOT_BASE
        for image in SQUASHFS_CANDIDATES:
            if os.path.isfile(image):
                self.r.mkdir(SOURCE_MOUNT)
                self.r.run(["mount", "-o", "loop,ro", "-t", "squashfs", image, SOURCE_MOUNT])
                self._mounted_source = True
                return SOURCE_MOUNT
        self.r.log("Live image not found, copying the running system instead")
        return "/"

    def copy_system(self) -> None:
        self.source = self.source or self._find_source()
        cmd = ["rsync", "-aHAX", "--numeric-ids", "--info=progress2", "--no-inc-recursive"]
        for pattern in RSYNC_EXCLUDES:
            cmd.append(f"--exclude={pattern}")
        live_user = self.edition.get("LIVE_USER", "live")
        cmd.append(f"--exclude=/home/{live_user}")
        cmd += [self.source.rstrip("/") + "/", self.target.rstrip("/") + "/"]

        last = [-1]

        def on_output(line: str) -> None:
            pct = parse_rsync_progress(line)
            if pct is None:
                self.r.log(line)
            elif pct != last[0]:
                last[0] = pct
                self._report(pct / 100.0, self._current_weight, f"Copying the system... {pct}%")

        self.r.run(cmd, on_output=on_output)
        for d in ("proc", "sys", "dev", "run", "tmp", "mnt", "media", "var/tmp"):
            self.r.mkdir(self.t(d))
        if not self.r.dry_run:
            os.chmod(self.t("/tmp"), 0o1777)
            os.chmod(self.t("/var/tmp"), 0o1777)

    def _mount_chroot(self) -> None:
        for fs in ("dev", "proc", "sys", "run"):
            dst = self.t(fs)
            if fs == "proc":
                self.r.run(["mount", "-t", "proc", "proc", dst])
            else:
                self.r.run(["mount", "--rbind", f"/{fs}", dst])
                self.r.run(["mount", "--make-rslave", dst])
            self._chroot_mounts.append(dst)

    def configure(self) -> None:
        self._mount_chroot()
        steps = (
            self._write_fstab, self._remove_live_session, self._set_hostname, self._set_timezone,
            self._set_locale, self._set_keyboard, self._create_user, self._tune_make_conf,
            self._set_autologin, self._setup_zfs, self._setup_snapshots, self._grub_defaults,
        )
        for i, step in enumerate(steps):
            step()
            self._report((i + 1) / len(steps), self._current_weight, self._current_text)

    def _uuid(self, dev: str) -> str:
        uuid = self.r.run(["blkid", "-s", "UUID", "-o", "value", dev], capture=True).strip()
        return uuid or f"DRY-RUN-UUID-OF-{os.path.basename(dev)}"

    def _write_fstab(self) -> None:
        lines = ["# /etc/fstab: static file system information (written by the installer)",
                 "# <fs>  <mountpoint>  <type>  <opts>  <dump>  <pass>"]
        if self.cfg.filesystem == "zfs":
            lines.append(f"# ZFS datasets of pool {ZFS_POOL} are mounted by the initramfs and zfs-mount")
        root_uuid = "" if self.cfg.filesystem == "zfs" else self._uuid(self.root_dev)
        if self.cfg.filesystem == "zfs":
            pass
        elif self.cfg.filesystem == "btrfs":
            for sub, mountpoint in BTRFS_SUBVOLUMES:
                lines.append(f"UUID={root_uuid}  {mountpoint}  btrfs  subvol={sub},{BTRFS_OPTS}  0 0")
        else:
            lines.append(f"UUID={root_uuid}  /  ext4  noatime  0 1")
        if self.boot_dev:
            lines.append(f"UUID={self._uuid(self.boot_dev)}  /boot  ext4  noatime  0 2")
        if self.efi_dev:
            lines.append(f"UUID={self._uuid(self.efi_dev)}  /efi  vfat  umask=0077  0 2")
        self.r.write_file(self.t("/etc/fstab"), "\n".join(lines) + "\n")

    def _remove_live_session(self) -> None:
        listing = self.r.read_file(self.live_files) or ""
        for rel in listing.splitlines():
            rel = rel.strip()
            if rel.startswith("/") and ".." not in rel:
                self.r.remove(self.t(rel))
        live_user = self.edition.get("LIVE_USER", "live")
        self.chroot("userdel", "--remove", live_user, check=False)
        self.r.remove(self.t(f"/home/{live_user}"))

    def _set_hostname(self) -> None:
        h = self.cfg.hostname
        self.r.write_file(self.t("/etc/hostname"), h + "\n")
        self.r.write_file(self.t("/etc/conf.d/hostname"), f'# Set the system hostname\nhostname="{h}"\n')
        self.r.write_file(
            self.t("/etc/hosts"),
            f"127.0.0.1\tlocalhost\n::1\t\tlocalhost\n127.0.1.1\t{h}.localdomain\t{h}\n",
        )

    def _set_timezone(self) -> None:
        tz = self.cfg.timezone
        self.r.write_file(self.t("/etc/timezone"), tz + "\n")
        self.r.symlink(f"../usr/share/zoneinfo/{tz}", self.t("/etc/localtime"))

    def _set_locale(self) -> None:
        loc = self.cfg.locale
        wanted = ["en_US.UTF-8 UTF-8"]
        if loc != "en_US.UTF-8":
            wanted.insert(0, f"{loc} UTF-8")
        self.r.write_file(self.t("/etc/locale.gen"), "\n".join(wanted) + "\n")
        self.chroot("locale-gen")
        self.r.write_file(self.t("/etc/env.d/02locale"), f'LANG="{loc}"\nLC_COLLATE="C.UTF-8"\n')
        self.chroot("env-update")

    def _set_keyboard(self) -> None:
        layout, variant = self.cfg.keyboard_layout, self.cfg.keyboard_variant
        self.r.write_file(
            self.t("/etc/conf.d/keymaps"),
            f'keymap="{system.console_keymap(layout)}"\nwindowkeys="YES"\nfix_euro="NO"\n',
        )
        self.r.write_file(
            self.t("/etc/X11/xorg.conf.d/00-keyboard.conf"),
            'Section "InputClass"\n'
            '\tIdentifier "system-keyboard"\n'
            '\tMatchIsKeyboard "on"\n'
            f'\tOption "XkbLayout" "{layout}"\n'
            + (f'\tOption "XkbVariant" "{variant}"\n' if variant else "")
            + "EndSection\n",
        )

    def _existing_groups(self) -> list[str]:
        text = self.r.read_file(self.t("/etc/group"))
        if text is None:
            return list(USER_GROUPS)
        present = {line.split(":", 1)[0] for line in text.splitlines() if line}
        return [g for g in USER_GROUPS if g in present]

    def _create_user(self) -> None:
        c = self.cfg
        self.chroot(
            "useradd", "--create-home", "--shell", "/bin/bash",
            "--groups", ",".join(self._existing_groups()),
            "--comment", c.full_name or c.username, c.username,
        )
        credentials = f"{c.username}:{c.password}\n"
        if c.root_password_same:
            credentials += f"root:{c.password}\n"
        self.chroot("chpasswd", input=credentials)
        if not c.root_password_same:
            self.chroot("passwd", "--lock", "root")

        # Plasma reads the keyboard layout per user.
        home = f"/home/{c.username}"
        kxkb = "[Layout]\nUse=true\nLayoutList=" + c.keyboard_layout + "\n"
        if c.keyboard_variant:
            kxkb += f"VariantList={c.keyboard_variant}\n"
        self.r.write_file(self.t(f"{home}/.config/kxkbrc"), kxkb)
        self.chroot("chown", "-R", f"{c.username}:{c.username}", f"{home}/.config")

    def _tune_make_conf(self) -> None:
        path = self.t("/etc/portage/make.conf")
        text = self.r.read_file(path)
        cpu = system.cpu_info()
        jobs = system.recommended_jobs(cpu.threads, system.mem_total_bytes())
        if text is None:
            self.r.log(f"(make.conf not found, would set MAKEOPTS=-j{jobs})")
            return
        new = re.sub(r'^MAKEOPTS=.*$', f'MAKEOPTS="-j{jobs} -l{jobs}"', text, flags=re.M)
        self.r.write_file(path, new)

    def _set_autologin(self) -> None:
        if self.cfg.autologin:
            self.r.write_file(
                self.t("/etc/sddm.conf.d/20-autologin.conf"),
                f"[Autologin]\nUser={self.cfg.username}\nSession=plasma\nRelogin=false\n",
            )

    def _setup_zfs(self) -> None:
        if self.cfg.filesystem != "zfs":
            return
        self.r.mkdir(self.t("/etc/zfs"))
        self.r.run(["cp", "/etc/hostid", self.t("/etc/hostid")])
        self.r.run(["cp", "/etc/zfs/zpool.cache", self.t("/etc/zfs/zpool.cache")])
        self.r.write_file(
            self.t("/etc/dracut.conf.d/20-zfs.conf"),
            "# Root file system on ZFS (written by the installer)\n"
            'add_dracutmodules+=" zfs "\n'
            'install_items+=" /etc/hostid /etc/zfs/zpool.cache "\n',
        )
        for service, runlevel in (("zfs-import", "boot"), ("zfs-mount", "boot"), ("zfs-zed", "default")):
            self.chroot("rc-update", "add", service, runlevel, check=False)

    def _setup_snapshots(self) -> None:
        backend = self.cfg.snapshot_backend()
        conf = f'# Automatic snapshots, set up by the installer: snapper (Btrfs), zfs or none.\nSNAPSHOTS="{backend}"\n'
        if backend == "zfs":
            conf += f'ZFS_DATASETS="{ZFS_ROOT} {ZFS_POOL}/home"\n'
        self.r.write_file(self.t("/etc/conf.d/gentoo-snapshots"), conf)
        if backend == "snapper":
            self.r.write_file(self.t("/etc/snapper/configs/root"), SNAPPER_CONFIG)
            self.r.write_file(self.t("/etc/conf.d/snapper"), 'SNAPPER_CONFIGS="root"\n')
            self.chroot("chmod", "750", "/.snapshots")
            self.chroot("snapper", "--no-dbus", "--config", "root", "create",
                        "--cleanup-algorithm", "number", "--description", "Fresh install", check=False)
        elif backend == "zfs":
            self.r.run(["zfs", "snapshot", "-r", f"{ZFS_POOL}@fresh-install"], check=False)

    def _kernel_versions(self) -> list[str]:
        try:
            return sorted(os.listdir(self.t("/lib/modules")))
        except OSError:
            return []

    def _grub_defaults(self) -> None:
        path = self.t("/etc/default/grub")
        text = self.r.read_file(path)
        if text is None:
            return
        name = self.edition.get("DISTRO_SHORT", "Gentoo")
        new = re.sub(r"^GRUB_DISTRIBUTOR=.*$", f'GRUB_DISTRIBUTOR="{name}"', text, flags=re.M)
        cmdline = []
        if self.cfg.encrypt:
            # dracut unlocks the root file system and asks for the passphrase
            # using the chosen keyboard layout.
            if self.cfg.filesystem != "zfs":
                cmdline.append(f"rd.luks.uuid={self._uuid(self.luks_dev)}")
            cmdline.append(f"rd.vconsole.keymap={system.console_keymap(self.cfg.keyboard_layout)}")
        if cmdline:
            new = self._set_grub_var(new, "GRUB_CMDLINE_LINUX", " ".join(cmdline))
        if self.cfg.filesystem == "zfs":
            # grub-mkconfig cannot describe a ZFS root on its own (and GRUB may
            # not read this pool's features); /etc/default/grub is read after
            # its probing, so these values win. root=zfs:... is dracut syntax.
            new = self._set_grub_var(new, "GRUB_DEVICE", f"zfs:{ZFS_ROOT}")
            new = self._set_grub_var(new, "GRUB_FS", "zfs-dracut")
            new = self._set_grub_var(new, "GRUB_DISABLE_LINUX_UUID", "true")
        self.r.write_file(path, new)

    @staticmethod
    def _set_grub_var(text: str, key: str, value: str) -> str:
        line = f'{key}="{value}"'
        new, found = re.subn(rf"^{key}=.*$", line, text, flags=re.M)
        return new if found else new.rstrip("\n") + "\n" + line + "\n"

    def bootloader(self) -> None:
        name = self.edition.get("DISTRO_SHORT", "Gentoo")
        if self.cfg.filesystem == "zfs":
            # Rebuild the initramfs with the zfs module, host id and pool cache.
            kernels = self._kernel_versions()
            if not kernels and not self.r.dry_run:
                raise InstallError("No kernel found in /lib/modules of the installed system.")
            for kver in kernels:
                self.chroot("dracut", "--force", "--kver", kver, f"/boot/initramfs-{kver}.img")
        if self.cfg.uefi:
            self.chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/efi",
                        f"--bootloader-id={name}", "--recheck")
            if self.cfg.mode == "erase":
                # Fallback loader (EFI/BOOT/BOOTX64.EFI) for firmwares that
                # forget boot entries. Not done on shared ESPs (dual boot).
                self.chroot("grub-install", "--target=x86_64-efi", "--efi-directory=/efi",
                            "--removable", "--recheck")
        else:
            self.chroot("grub-install", "--target=i386-pc", "--recheck", self.cfg.disk)
        if self.cfg.filesystem != "zfs":
            self._ensure_uuid_link(self.root_dev)
        self.chroot("grub-mkconfig", "-o", "/boot/grub/grub.cfg")

    def _ensure_uuid_link(self, dev: str) -> None:
        """Make sure /dev/disk/by-uuid knows the root file system.

        Without the link (udev slow or not running), grub-mkconfig falls back
        to root=/dev/<name of the disk right now>, which may not exist on the
        next boot.
        """
        self.r.run(["udevadm", "settle"], check=False)
        uuid = self._uuid(dev)
        link = os.path.join(DEV_BY_UUID, uuid)
        if self.r.dry_run or os.path.lexists(link):
            return
        self.r.log(f"udev has not created {link} yet; creating it so GRUB uses root=UUID=")
        self.r.mkdir(DEV_BY_UUID)
        self.r.symlink(os.path.realpath(dev), link)

    def finish(self) -> None:
        self.r.run(["sync"], check=False)

    def _stop_target_processes(self) -> None:
        """Stop processes still running inside the new system.

        A tool run in the chroot can leave a helper behind (an agent, a cache
        daemon). It keeps the file systems busy, so they could not be
        unmounted, the encrypted volume closed or the ZFS pool exported.
        """
        if self.r.dry_run:
            return

        def rooted_in_target() -> list[int]:
            pids = []
            for entry in os.listdir("/proc"):
                if not entry.isdigit() or int(entry) == os.getpid():
                    continue
                try:
                    root = os.readlink(f"/proc/{entry}/root")
                except OSError:
                    continue
                if root == self.target or root.startswith(self.target + "/"):
                    pids.append(int(entry))
            return pids

        for sig in (signal.SIGTERM, signal.SIGKILL):
            pids = rooted_in_target()
            if not pids:
                return
            for pid in pids:
                try:
                    name = Path(f"/proc/{pid}/comm").read_text().strip()
                    self.r.log(f"stopping leftover process {pid} ({name}) in the new system")
                    os.kill(pid, sig)
                except OSError:
                    pass
            time.sleep(1)

    def cleanup(self) -> None:
        """Unmount everything; safe to call more than once and after errors."""
        self._stop_target_processes()
        for mp in reversed(self._chroot_mounts):
            if not self.r.succeeds(["umount", "--recursive", mp]):
                self.r.run(["umount", "--recursive", "--lazy", mp], check=False)
        self._chroot_mounts = []
        if self.root_dev:
            self.r.run(["umount", "--recursive", self.target], check=False)
        if self._target_private:  # the private bind mount below everything, if still there
            if not self.r.dry_run and os.path.ismount(self.target):
                self.r.run(["umount", self.target], check=False)
            self._target_private = False
        if self._zpool_created:
            self.r.run(["zpool", "export", ZFS_POOL], check=False)
            self._zpool_created = False
        if self._luks_open:
            for _ in range(5):  # udev may still hold the device for a moment
                if self.r.succeeds(["cryptsetup", "close", LUKS_NAME]):
                    break
                self.r.run(["udevadm", "settle"], check=False)
                if not self.r.dry_run:
                    time.sleep(1)
            else:
                self.r.log(f"Could not close /dev/mapper/{LUKS_NAME}; it is closed at the next reboot")
            self._luks_open = False
        if self._mounted_source:
            self.r.run(["umount", SOURCE_MOUNT], check=False)
            self._mounted_source = False
