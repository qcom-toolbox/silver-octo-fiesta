import shlex

import pytest

from am4_installer import backend, system

EDITION = {
    "DISTRO_NAME": "Gentoo Linux", "DISTRO_SHORT": "Gentoo", "DISTRO_ID": "gentoo",
    "EDITION": "zen3-nvidia", "CPU_DESC": "AMD Zen 3", "CPU_MARCH": "znver3",
    "CPU_MIN_FAMILY": "25", "GPU_ID": "nvidia", "LIVE_USER": "live",
}


class FakeRunner(backend.Runner):
    """Records commands instead of running them; file operations are real (in tmp_path)."""

    def __init__(self):
        self.lines = []
        super().__init__(dry_run=False, log=self.lines.append)
        self.commands = []
        self.inputs = []

    def run(self, cmd, *, input=None, check=True, capture=False, on_output=None):
        self.log("$ " + shlex.join(cmd))
        self.commands.append(list(cmd))
        self.inputs.append(input)
        if on_output:
            on_output("  1,000  50%  1.00MB/s  0:00:01")
        return ""


def make_config(**overrides):
    cfg = backend.InstallConfig(
        locale="de_DE.UTF-8", timezone="Europe/Berlin", keyboard_layout="de",
        keyboard_variant="nodeadkeys", mode="erase", disk="/dev/nvme0n1", filesystem="btrfs",
        full_name="Alex Example", username="alex", password="s3cret!", hostname="ryzen-pc",
        autologin=True, uefi=True,
    )
    for key, value in overrides.items():
        setattr(cfg, key, value)
    return cfg


@pytest.fixture
def target(tmp_path):
    root = tmp_path / "target"
    (root / "etc/portage").mkdir(parents=True)
    (root / "etc/default").mkdir(parents=True)
    (root / "etc/portage/make.conf").write_text('CFLAGS="-O2"\nMAKEOPTS="-j32 -l32"\n')
    (root / "etc/default/grub").write_text('GRUB_DISTRIBUTOR="X"\nGRUB_TIMEOUT=3\n')
    (root / "etc/group").write_text("root:x:0:\nwheel:x:10:\naudio:x:18:\nvideo:x:27:\nusers:x:100:\n")
    (root / "etc/sddm.conf.d").mkdir()
    (root / "etc/sddm.conf.d/50-live-autologin.conf").write_text("[Autologin]\nUser=live\n")
    (root / "usr/share/applications").mkdir(parents=True)
    (root / "usr/share/applications/am4-installer.desktop").write_text("x")
    listing = tmp_path / "live-files.list"
    listing.write_text("/etc/sddm.conf.d/50-live-autologin.conf\n/usr/share/applications/am4-installer.desktop\n")
    return root, listing


def run_install(cfg, target):
    root, listing = target
    runner = FakeRunner()
    progress = []
    inst = backend.Installer(cfg, runner, progress=lambda f, t: progress.append(f),
                             edition=EDITION, target=str(root), live_files=str(listing))
    inst.run()
    return runner, progress, root


def test_erase_install_uefi_btrfs(target):
    runner, progress, root = run_install(make_config(), target)
    cmds = [" ".join(c) for c in runner.commands]

    assert "wipefs --all --force /dev/nvme0n1" in cmds
    sfdisk = runner.commands.index(next(c for c in runner.commands if c[0] == "sfdisk"))
    assert "type=U" in runner.inputs[sfdisk] and "label: gpt" in runner.inputs[sfdisk]
    assert "mkfs.vfat -F 32 -n EFI /dev/nvme0n1p1" in cmds
    assert "mkfs.btrfs --force --label Gentoo /dev/nvme0n1p2" in cmds
    assert f"mount -o subvol=@,{backend.BTRFS_OPTS} /dev/nvme0n1p2 {root}" in cmds
    assert any(c[0] == "rsync" and "--exclude=/home/live" in c for c in runner.commands)
    assert any("--bootloader-id=Gentoo" in c for c in cmds)
    assert any("--removable" in c for c in cmds)
    assert any(c.startswith("umount --recursive " + str(root)) for c in cmds)

    fstab = (root / "etc/fstab").read_text()
    assert "subvol=@," in fstab and "subvol=@home," in fstab and "/efi  vfat" in fstab
    assert (root / "etc/hostname").read_text() == "ryzen-pc\n"
    assert 'keymap="de"' in (root / "etc/conf.d/keymaps").read_text()
    assert "LayoutList=de" in (root / "home/alex/.config/kxkbrc").read_text()
    assert "VariantList=nodeadkeys" in (root / "home/alex/.config/kxkbrc").read_text()
    assert "User=alex" in (root / "etc/sddm.conf.d/20-autologin.conf").read_text()
    assert not (root / "etc/sddm.conf.d/50-live-autologin.conf").exists()
    assert not (root / "usr/share/applications/am4-installer.desktop").exists()
    assert (root / "etc/localtime").readlink().as_posix() == "../usr/share/zoneinfo/Europe/Berlin"
    assert "de_DE.UTF-8 UTF-8" in (root / "etc/locale.gen").read_text()
    assert 'GRUB_DISTRIBUTOR="Gentoo"' in (root / "etc/default/grub").read_text()
    assert "-j32" not in (root / "etc/portage/make.conf").read_text()

    useradd = next(c for c in runner.commands if "useradd" in c)
    assert useradd[useradd.index("--groups") + 1] == "users,wheel,audio,video"
    assert "alex:s3cret!\n" in runner.inputs
    assert any(c[-2:] == ["--lock", "root"] for c in runner.commands)
    # the password never ends up in the log
    assert not any("s3cret" in line for line in runner.lines)

    assert progress[-1] == 1.0
    assert progress == sorted(progress)


def test_manual_install_keeps_efi(target):
    cfg = make_config(mode="manual", disk="/dev/sda", root_partition="/dev/sda3",
                      efi_partition="/dev/sda1", format_efi=False, filesystem="ext4",
                      root_password_same=True, autologin=False)
    runner, _, root = run_install(cfg, target)
    cmds = [" ".join(c) for c in runner.commands]
    assert not any(c.startswith(("wipefs", "sfdisk", "mkfs.vfat")) for c in cmds)
    assert "mkfs.ext4 -F -L Gentoo /dev/sda3" in cmds
    assert not any("--removable" in c for c in cmds)  # never overwrite a shared ESP's fallback
    assert "root:s3cret!" in "".join(i or "" for i in runner.inputs)
    assert not (root / "etc/sddm.conf.d/20-autologin.conf").exists()
    assert "/  ext4  noatime  0 1" in (root / "etc/fstab").read_text()


def test_bios_install(target):
    cfg = make_config(uefi=False, disk="/dev/sda")
    runner, _, root = run_install(cfg, target)
    sfdisk_input = next(i for c, i in zip(runner.commands, runner.inputs) if c[0] == "sfdisk")
    assert backend.BIOS_BOOT_GUID in sfdisk_input
    assert ["chroot", str(root), "grub-install", "--target=i386-pc", "--recheck", "/dev/sda"] in runner.commands
    assert "/efi" not in (root / "etc/fstab").read_text()


@pytest.mark.parametrize("field,value", [
    ("username", "Root"), ("username", "live"), ("hostname", "-bad"), ("password", ""),
    ("mode", "manual"), ("disk", ""),
])
def test_invalid_config_is_rejected_before_touching_disks(target, field, value):
    root, listing = target
    runner = FakeRunner()
    inst = backend.Installer(make_config(**{field: value}), runner, edition=EDITION,
                             target=str(root), live_files=str(listing))
    with pytest.raises(backend.InstallError):
        inst.run()
    assert not any(c[0] in ("wipefs", "sfdisk") for c in runner.commands)


def test_dry_run_logs_only(tmp_path, capsys):
    lines = []
    runner = backend.Runner(dry_run=True, log=lines.append)
    inst = backend.Installer(make_config(), runner, edition=EDITION, target=str(tmp_path / "t"),
                             live_files=str(tmp_path / "missing"))
    inst.run()
    assert not (tmp_path / "t").exists()
    assert any(line.startswith("$ sfdisk") for line in lines)


def test_partition_path():
    assert backend.partition_path("/dev/sda", 1) == "/dev/sda1"
    assert backend.partition_path("/dev/nvme0n1", 2) == "/dev/nvme0n1p2"
    assert backend.partition_path("/dev/mmcblk0", 1) == "/dev/mmcblk0p1"


def test_rsync_progress():
    assert backend.parse_rsync_progress("  1,234,567,890  45%   98.10MB/s    0:00:12 (xfr#1, to-chk=0/2)") == 45
    assert backend.parse_rsync_progress("sending incremental file list") is None
    assert backend.parse_rsync_progress("100% done") == 100


def test_validators():
    assert backend.validate_username("alex") is None
    assert backend.validate_username("9lives")
    assert backend.validate_hostname("my-ryzen") is None
    assert backend.validate_hostname("a" * 64)
    assert backend.validate_password("a", "b")
    assert backend.validate_password("a:b", "a:b")
    assert backend.suggest_username("Sam Smith") == "sam"
    assert backend.suggest_username("42 Things") == "u42"


CPUINFO_2600X = """processor\t: 0
vendor_id\t: AuthenticAMD
cpu family\t: 23
model name\t: AMD Ryzen 5 2600X Six-Core Processor
processor\t: 1
vendor_id\t: AuthenticAMD
cpu family\t: 23
model name\t: AMD Ryzen 5 2600X Six-Core Processor
"""


def test_cpu_compatibility():
    cpu = system.parse_cpuinfo(CPUINFO_2600X)
    assert (cpu.family, cpu.threads, cpu.vendor) == (23, 2, "AuthenticAMD")
    zen3 = {"CPU_MARCH": "znver3", "CPU_MIN_FAMILY": "25", "CPU_DESC": "Zen 3"}
    zenplus = {"CPU_MARCH": "znver1", "CPU_MIN_FAMILY": "23", "CPU_DESC": "Zen+"}
    assert system.cpu_compatibility(zen3, cpu)[0] == "error"
    assert system.cpu_compatibility(zenplus, cpu) is None
    zen3_cpu = system.CpuInfo(vendor="AuthenticAMD", model="AMD Ryzen 9 5950X", family=25, threads=32)
    assert system.cpu_compatibility(zen3, zen3_cpu) is None
    assert system.cpu_compatibility(zenplus, zen3_cpu)[0] == "warning"
    intel = system.CpuInfo(vendor="GenuineIntel", model="Intel", family=6)
    assert system.cpu_compatibility(zenplus, intel)[0] == "error"


def test_recommended_jobs():
    gib = system.GIB
    assert system.recommended_jobs(32, 64 * gib) == 32
    assert system.recommended_jobs(32, 16 * gib) == 8
    assert system.recommended_jobs(12, 1 * gib) == 1


def test_parse_lsblk_skips_live_medium_and_zram():
    data = {"blockdevices": [
        {"path": "/dev/nvme0n1", "size": 1000204886016, "model": "Samsung SSD 980 1TB ", "type": "disk",
         "tran": "nvme", "rm": False, "ro": False, "children": [
             {"path": "/dev/nvme0n1p1", "size": 104857600, "type": "part", "fstype": "vfat",
              "parttype": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B"},
         ]},
        {"path": "/dev/sdb", "size": 32000000000, "model": "USB Stick", "type": "disk", "tran": "usb",
         "rm": True, "ro": False, "children": [
             {"path": "/dev/sdb1", "size": 4000000000, "type": "part", "mountpoint": "/run/initramfs/live"},
         ]},
        {"path": "/dev/zram0", "size": 8000000000, "type": "disk", "ro": False},
    ]}
    disks = system.parse_lsblk(data)
    assert [d.path for d in disks] == ["/dev/nvme0n1", "/dev/sdb"]
    assert disks[0].partitions[0].is_efi
    assert disks[0].describe() == "Samsung SSD 980 1TB - /dev/nvme0n1 (931.5 GiB, NVMe SSD)"
    assert disks[1].is_live_medium and not disks[0].is_live_medium


def test_xkb_layouts_and_keymaps():
    text = "! model\n  pc105  Generic 105-key PC\n\n! layout\n  us   English (US)\n  gb   English (UK)\n\n! variant\n  intl  us: x\n"
    assert system.parse_xkb_layouts(text) == [("us", "English (US)"), ("gb", "English (UK)")]
    assert system.console_keymap("gb") == "uk"
    assert system.console_keymap("de") == "de"


def test_read_edition(tmp_path):
    f = tmp_path / "edition.conf"
    f.write_text('DISTRO_NAME="Gentoo Linux"\nEDITION="zen3-nvidia"\nCPU_MIN_FAMILY=25\n')
    ed = system.read_edition(str(f))
    assert ed["DISTRO_NAME"] == "Gentoo Linux" and ed["CPU_MIN_FAMILY"] == "25"
    assert system.read_edition(str(tmp_path / "nope"))["DISTRO_ID"] == "gentoo"
