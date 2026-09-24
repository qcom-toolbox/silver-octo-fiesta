"""Read-only information about the machine the installer runs on."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

EDITION_FILE = "/usr/share/gentoo-desktop/edition.conf"
EFI_PARTTYPE = "c12a7328-f81f-11d2-ba4b-00a0c93ec93b"
# Where dracut's dmsquash-live mounts the boot medium.
LIVE_MEDIUM_MOUNT = "/run/initramfs/live"

GIB = 1024**3


# ---------------------------------------------------------------------------
# Edition
# ---------------------------------------------------------------------------
def read_edition(path: str = EDITION_FILE) -> dict[str, str]:
    """Parse the KEY="value" file written by the build (see build-system.sh)."""
    values = {
        "DISTRO_NAME": "Gentoo Linux",
        "DISTRO_SHORT": "Gentoo",
        "DISTRO_ID": "gentoo",
        "EDITION": "unknown",
        "CPU_DESC": "unknown CPU edition",
        "CPU_ID": "",
        "CPU_VENDOR": "",
        "CPU_CFLAGS": "",
        "CPU_REQUIRED_FLAGS": "",
        "GPU_ID": "",
        "GPU_DESC": "",
        "LIVE_USER": "live",
    }
    try:
        text = Path(path).read_text()
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, raw = line.partition("=")
        try:
            parts = shlex.split(raw)
        except ValueError:
            continue
        values[key.strip()] = parts[0] if parts else ""
    return values


# ---------------------------------------------------------------------------
# CPU / GPU / memory
# ---------------------------------------------------------------------------
@dataclass
class CpuInfo:
    vendor: str = ""
    model: str = "Unknown CPU"
    family: int = 0
    threads: int = 1
    flags: frozenset[str] = frozenset()


def parse_cpuinfo(text: str) -> CpuInfo:
    info = CpuInfo(threads=0)
    for line in text.splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if key == "processor":
            info.threads += 1
        elif key == "vendor_id" and not info.vendor:
            info.vendor = value
        elif key == "model name" and info.model == "Unknown CPU":
            info.model = value
        elif key == "cpu family" and not info.family:
            try:
                info.family = int(value)
            except ValueError:
                pass
        elif key == "flags" and not info.flags:
            info.flags = frozenset(value.split())
    info.threads = max(info.threads, 1)
    return info


def cpu_info() -> CpuInfo:
    try:
        return parse_cpuinfo(Path("/proc/cpuinfo").read_text())
    except OSError:
        return CpuInfo(threads=os.cpu_count() or 1)


# Editions built by build.sh (config/cpu/*.conf), for messages.
CPU_EDITIONS = {
    "zenplus": "AMD Ryzen 1000-3000 (zenplus)",
    "zen3": "AMD Ryzen 5000 (zen3)",
    "zen4": "AMD Ryzen 7000 (zen4)",
    "zen5": "AMD Ryzen 9000 (zen5)",
    "intel": "Intel Core 10th gen+ (intel)",
}
_X86_64_V3 = {"avx2", "fma", "bmi1", "bmi2", "movbe", "f16c"}


def recommended_cpu_edition(cpu: CpuInfo) -> str | None:
    """The best tuned edition for this CPU, or None if no edition runs on it."""
    if cpu.flags and not _X86_64_V3 <= cpu.flags:
        return None  # no AVX2 (e.g. Pentium/Celeron): too old for every edition
    if cpu.vendor == "GenuineIntel":
        return "intel"
    if cpu.vendor == "AuthenticAMD":
        if cpu.family >= 26:
            return "zen5"
        if cpu.family == 25:
            return "zen4" if "avx512f" in cpu.flags else "zen3"
        if cpu.family == 23:
            return "zenplus"
    return None


def cpu_compatibility(edition: dict[str, str], cpu: CpuInfo) -> tuple[str, str] | None:
    """Return (severity, message) if this CPU is a poor or impossible match.

    severity is "error" when the installed system would crash with
    "Illegal instruction", "warning" when it works but another edition is
    better tuned for this CPU.
    """
    required = set(edition.get("CPU_REQUIRED_FLAGS", "").split())
    if not edition.get("CPU_ID") or not cpu.flags:
        return None  # unknown edition or CPU: nothing to compare
    best = recommended_cpu_edition(cpu)
    use_instead = (f" Use the {CPU_EDITIONS.get(best, best)} edition instead."
                   if best else " No edition supports this processor.")
    missing = sorted(required - cpu.flags)
    if missing:
        return (
            "error",
            f"This image is built for {edition.get('CPU_DESC')} and uses instructions your "
            f"{cpu.model} does not have ({', '.join(missing)}). Programs would crash with "
            f"'Illegal instruction'.{use_instead}",
        )
    if best and best != edition["CPU_ID"]:
        return (
            "warning",
            f"This image works on your {cpu.model}, but the {CPU_EDITIONS.get(best, best)} "
            "edition is tuned for it and runs faster.",
        )
    return None


@dataclass
class Gpu:
    vendor: str
    name: str
    slot: str = ""


PCI_VENDORS = {"0x10de": "NVIDIA", "0x1002": "AMD", "0x8086": "Intel"}


def detect_gpus(sysfs: str = "/sys/bus/pci/devices") -> list[Gpu]:
    gpus: list[Gpu] = []
    try:
        slots = sorted(os.listdir(sysfs))
    except OSError:
        return gpus
    for slot in slots:
        base = Path(sysfs) / slot
        try:
            pci_class = (base / "class").read_text().strip()
            vendor_id = (base / "vendor").read_text().strip().lower()
        except OSError:
            continue
        if not pci_class.startswith("0x03"):  # display controllers
            continue
        vendor = PCI_VENDORS.get(vendor_id, vendor_id)
        gpus.append(Gpu(vendor=vendor, name=_lspci_name(slot) or f"{vendor} graphics", slot=slot))
    return gpus


def _lspci_name(slot: str) -> str:
    try:
        out = subprocess.run(
            ["lspci", "-s", slot, "-mm"], capture_output=True, text=True, timeout=5, check=False
        ).stdout
        fields = shlex.split(out)
        # slot, class, vendor, device, ...
        if len(fields) >= 4:
            return f"{fields[2]} {fields[3]}"
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    return ""


def mem_total_bytes(meminfo: str = "/proc/meminfo") -> int:
    try:
        for line in Path(meminfo).read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def recommended_jobs(threads: int, mem_bytes: int) -> int:
    """Parallel compile jobs: one per thread, but at most one per 2 GiB of RAM."""
    by_mem = int(mem_bytes // (2 * GIB)) if mem_bytes else threads
    return max(1, min(threads, by_mem))


def is_uefi() -> bool:
    return os.path.isdir("/sys/firmware/efi")


# ---------------------------------------------------------------------------
# Disks
# ---------------------------------------------------------------------------
@dataclass
class Partition:
    path: str
    size: int
    fstype: str = ""
    label: str = ""
    parttype: str = ""
    disk: str = ""

    @property
    def is_efi(self) -> bool:
        return self.parttype.lower() == EFI_PARTTYPE

    def describe(self) -> str:
        bits = [self.path, human_size(self.size)]
        if self.fstype:
            bits.append(self.fstype)
        if self.label:
            bits.append(f'"{self.label}"')
        if self.is_efi:
            bits.append("EFI system partition")
        return " - ".join(bits)


@dataclass
class Disk:
    path: str
    size: int
    model: str = ""
    transport: str = ""
    removable: bool = False
    is_live_medium: bool = False
    partitions: list[Partition] = field(default_factory=list)

    def describe(self) -> str:
        kind = {"nvme": "NVMe SSD", "sata": "SATA", "usb": "USB"}.get(self.transport, self.transport)
        model = self.model or "Disk"
        extra = f", {kind}" if kind else ""
        return f"{model} - {self.path} ({human_size(self.size)}{extra})"


def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


LSBLK_COLUMNS = "PATH,SIZE,MODEL,TYPE,TRAN,RM,RO,FSTYPE,LABEL,PARTTYPE,MOUNTPOINT"


def parse_lsblk(data: dict) -> list[Disk]:
    disks: list[Disk] = []

    def mounted_at(node: dict, mountpoint: str) -> bool:
        if node.get("mountpoint") == mountpoint:
            return True
        return any(mounted_at(c, mountpoint) for c in node.get("children") or [])

    for dev in data.get("blockdevices", []):
        path = dev.get("path") or ""
        if dev.get("type") != "disk" or _truthy(dev.get("ro")):
            continue
        if any(path.startswith(p) for p in ("/dev/zram", "/dev/loop", "/dev/sr", "/dev/ram")):
            continue
        disk = Disk(
            path=path,
            size=int(dev.get("size") or 0),
            model=(dev.get("model") or "").strip(),
            transport=dev.get("tran") or "",
            removable=_truthy(dev.get("rm")),
            is_live_medium=mounted_at(dev, LIVE_MEDIUM_MOUNT),
        )
        for child in dev.get("children") or []:
            if child.get("type") != "part":
                continue
            disk.partitions.append(
                Partition(
                    path=child.get("path") or "",
                    size=int(child.get("size") or 0),
                    fstype=child.get("fstype") or "",
                    label=child.get("label") or "",
                    parttype=child.get("parttype") or "",
                    disk=path,
                )
            )
        disks.append(disk)
    return disks


def _truthy(value) -> bool:
    return value in (True, 1, "1", "true")


def list_disks() -> list[Disk]:
    try:
        out = subprocess.run(
            ["lsblk", "--json", "--bytes", "--output", LSBLK_COLUMNS],
            capture_output=True, text=True, check=True, timeout=15,
        ).stdout
        return parse_lsblk(json.loads(out))
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


# ---------------------------------------------------------------------------
# Localisation
# ---------------------------------------------------------------------------
FALLBACK_LOCALES = [
    "en_US.UTF-8", "en_GB.UTF-8", "de_DE.UTF-8", "fr_FR.UTF-8", "es_ES.UTF-8",
    "it_IT.UTF-8", "nl_NL.UTF-8", "pt_BR.UTF-8", "pl_PL.UTF-8", "ru_RU.UTF-8",
]


def list_locales(supported: str = "/usr/share/i18n/SUPPORTED") -> list[str]:
    try:
        lines = Path(supported).read_text().splitlines()
    except OSError:
        return FALLBACK_LOCALES
    locales = []
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1] == "UTF-8" and parts[0].endswith(".UTF-8"):
            locales.append(parts[0])
    return sorted(set(locales)) or FALLBACK_LOCALES


def list_timezones() -> list[str]:
    try:
        import zoneinfo

        zones = zoneinfo.available_timezones()
    except Exception:  # noqa: BLE001 - tzdata can be missing in odd setups
        zones = set()
    zones = {
        z for z in zones
        if "/" in z and not z.startswith(("Etc/", "posix/", "right/", "SystemV/", "US/"))
    }
    return ["UTC"] + sorted(zones)


def current_timezone() -> str:
    try:
        tz = Path("/etc/timezone").read_text().strip()
        if tz:
            return tz
    except OSError:
        pass
    try:
        link = os.readlink("/etc/localtime")
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass
    return "UTC"


FALLBACK_LAYOUTS = [
    ("us", "English (US)"), ("gb", "English (UK)"), ("de", "German"), ("fr", "French"),
    ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("br", "Portuguese (Brazil)"),
    ("se", "Swedish"), ("no", "Norwegian"), ("dk", "Danish"), ("fi", "Finnish"),
    ("pl", "Polish"), ("ru", "Russian"), ("ch", "German (Switzerland)"), ("be", "Belgian"),
]


def parse_xkb_layouts(text: str) -> list[tuple[str, str]]:
    layouts: list[tuple[str, str]] = []
    in_layouts = False
    for line in text.splitlines():
        if line.startswith("!"):
            in_layouts = line.strip() == "! layout"
            continue
        if in_layouts and line.strip():
            code, _, desc = line.strip().partition(" ")
            layouts.append((code, desc.strip()))
    return layouts


def list_keyboard_layouts(evdev: str = "/usr/share/X11/xkb/rules/evdev.lst") -> list[tuple[str, str]]:
    try:
        layouts = parse_xkb_layouts(Path(evdev).read_text())
    except OSError:
        layouts = []
    return sorted(layouts or FALLBACK_LAYOUTS, key=lambda item: item[1].lower())


# XKB layout -> Linux console keymap (only where the names differ).
CONSOLE_KEYMAPS = {
    "gb": "uk", "se": "sv-latin1", "pt": "pt-latin1", "br": "br-abnt2", "be": "be-latin1",
    "latam": "la-latin1", "tr": "trq", "jp": "jp106", "ch": "sg", "ca": "cf",
}


def console_keymap(layout: str) -> str:
    return CONSOLE_KEYMAPS.get(layout, layout or "us")
