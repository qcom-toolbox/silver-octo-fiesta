import os
import shlex

import pytest

from gentoo_installer import backend, system

EDITION = {
    "DISTRO_NAME": "Gentoo Linux", "DISTRO_SHORT": "Gentoo", "DISTRO_ID": "gentoo",
    "EDITION": "zen3-nvidia", "CPU_ID": "zen3", "CPU_DESC": "AMD Zen 3",
    "CPU_REQUIRED_FLAGS": "avx2 sha_ni vaes", "GPU_ID": "nvidia", "LIVE_USER": "live",
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
    (root / "usr/share/applications/gentoo-installer.desktop").write_text("x")
    listing = tmp_path / "live-files.list"
    listing.write_text("/etc/sddm.conf.d/50-live-autologin.conf\n/usr/share/applications/gentoo-installer.desktop\n")
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
    assert not (root / "usr/share/applications/gentoo-installer.desktop").exists()
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


def test_btrfs_subvolumes(target):
    runner, _, root = run_install(make_config(), target)
    cmds = [" ".join(c) for c in runner.commands]
    for sub, mountpoint in backend.BTRFS_SUBVOLUMES:
        assert f"btrfs subvolume create {root}/{sub}" in cmds
        path = str(root) if mountpoint == "/" else f"{root}{mountpoint}"
        assert f"mount -o subvol={sub},{backend.BTRFS_OPTS} /dev/nvme0n1p2 {path}" in cmds
    fstab = (root / "etc/fstab").read_text()
    for sub, mountpoint in backend.BTRFS_SUBVOLUMES:
        assert f"  {mountpoint}  btrfs  subvol={sub},{backend.BTRFS_OPTS}  0 0" in fstab
    # "/" is mounted before the subvolumes below it
    order = [c for c in cmds if c.startswith("mount -o subvol=")]
    assert order[0].endswith(str(root))


def test_luks_install(target):
    cfg = make_config(encrypt=True, luks_passphrase="correct horse battery")
    runner, _, root = run_install(cfg, target)
    cmds = [" ".join(c) for c in runner.commands]

    layout = next(i for c, i in zip(runner.commands, runner.inputs) if c[0] == "sfdisk")
    assert layout.count("\n") == 4 and 'name="boot"' in layout  # label + EFI + boot + root
    luks_format = cmds.index("cryptsetup luksFormat --type luks2 --batch-mode --key-file=- /dev/nvme0n1p3")
    luks_open = cmds.index("cryptsetup open --key-file=- /dev/nvme0n1p3 cryptroot")
    assert runner.inputs[luks_format] == runner.inputs[luks_open] == "correct horse battery"
    assert luks_format < luks_open < cmds.index("mkfs.btrfs --force --label Gentoo /dev/mapper/cryptroot")
    assert "mkfs.ext4 -F -L boot /dev/nvme0n1p2" in cmds
    assert "mkfs.vfat -F 32 -n EFI /dev/nvme0n1p1" in cmds
    assert f"mount -o noatime /dev/nvme0n1p2 {root}/boot" in cmds

    fstab = (root / "etc/fstab").read_text()
    assert "/boot  ext4" in fstab and "/efi  vfat" in fstab
    grub = (root / "etc/default/grub").read_text()
    assert 'GRUB_CMDLINE_LINUX="rd.luks.uuid=DRY-RUN-UUID-OF-nvme0n1p3 rd.vconsole.keymap=de"' in grub

    # closed again after unmounting, and the passphrase is never logged
    assert cmds.index("cryptsetup close cryptroot") > cmds.index(f"umount --recursive {root}")
    assert not any("correct horse" in line for line in runner.lines)


def test_luks_bios_layout(target):
    cfg = make_config(uefi=False, disk="/dev/sda", encrypt=True, luks_passphrase="12345678", filesystem="ext4")
    runner, _, root = run_install(cfg, target)
    cmds = [" ".join(c) for c in runner.commands]
    layout = next(i for c, i in zip(runner.commands, runner.inputs) if c[0] == "sfdisk")
    assert backend.BIOS_BOOT_GUID in layout
    assert "cryptsetup open --key-file=- /dev/sda3 cryptroot" in cmds
    assert "mkfs.ext4 -F -L Gentoo /dev/mapper/cryptroot" in cmds
    assert f"mount -o noatime /dev/sda2 {root}/boot" in cmds


def test_snapper_is_set_up_for_btrfs(target):
    runner, _, root = run_install(make_config(), target)
    assert 'SNAPSHOTS="snapper"' in (root / "etc/conf.d/gentoo-snapshots").read_text()
    config = (root / "etc/snapper/configs/root").read_text()
    assert 'SUBVOLUME="/"' in config and 'TIMELINE_CREATE="yes"' in config
    assert (root / "etc/conf.d/snapper").read_text() == 'SNAPPER_CONFIGS="root"\n'
    assert any("snapper" in c and "Fresh install" in c for c in runner.commands)


@pytest.mark.parametrize("overrides", [{"snapshots": False}, {"filesystem": "ext4"}])
def test_snapshots_off(target, overrides):
    runner, _, root = run_install(make_config(**overrides), target)
    assert 'SNAPSHOTS="none"' in (root / "etc/conf.d/gentoo-snapshots").read_text()
    assert not (root / "etc/snapper/configs/root").exists()
    assert not any("snapper" in c for c in runner.commands)


def test_zfs_install(target):
    root, _ = target
    (root / "lib/modules/6.12.40-gentoo-dist").mkdir(parents=True)
    runner, _, root = run_install(make_config(filesystem="zfs"), target)
    cmds = [" ".join(c) for c in runner.commands]

    layout = next(i for c, i in zip(runner.commands, runner.inputs) if c[0] == "sfdisk")
    assert 'name="boot"' in layout  # GRUB boots from a plain ext4 /boot
    assert "mkfs.ext4 -F -L boot /dev/nvme0n1p2" in cmds
    create = next(c for c in runner.commands if c[:2] == ["zpool", "create"])
    assert create[-2:] == ["rpool", "/dev/nvme0n1p3"]
    assert ["-R", str(root)] == create[create.index("-R"):create.index("-R") + 2]
    assert "encryption=on" not in create
    assert cmds.index("zgenhostid -f") < cmds.index(" ".join(create))
    assert cmds.index("zfs create -o canmount=noauto -o mountpoint=/ rpool/ROOT/gentoo") \
        < cmds.index("zfs mount rpool/ROOT/gentoo") < cmds.index("zfs create -o mountpoint=/home rpool/home")
    assert "zpool set bootfs=rpool/ROOT/gentoo rpool" in cmds
    assert not any(c.startswith(("mkfs.btrfs", "mount -o subvol")) for c in cmds)

    assert f"cp /etc/hostid {root}/etc/hostid" in cmds
    assert f"cp /etc/zfs/zpool.cache {root}/etc/zfs/zpool.cache" in cmds
    assert 'add_dracutmodules+=" zfs "' in (root / "etc/dracut.conf.d/20-zfs.conf").read_text()
    assert f"chroot {root} rc-update add zfs-import boot" in cmds
    assert f"chroot {root} dracut --force --kver 6.12.40-gentoo-dist /boot/initramfs-6.12.40-gentoo-dist.img" in cmds

    fstab = (root / "etc/fstab").read_text()
    entries = [line.split() for line in fstab.splitlines() if line and not line.startswith("#")]
    assert [e[1] for e in entries] == ["/boot", "/efi"]  # ZFS mounts / and the datasets itself
    grub = (root / "etc/default/grub").read_text()
    assert 'GRUB_DEVICE="zfs:rpool/ROOT/gentoo"' in grub and 'GRUB_DISABLE_LINUX_UUID="true"' in grub

    conf = (root / "etc/conf.d/gentoo-snapshots").read_text()
    assert 'SNAPSHOTS="zfs"' in conf and 'ZFS_DATASETS="rpool/ROOT/gentoo rpool/home"' in conf
    assert "zfs snapshot -r rpool@fresh-install" in cmds
    # exported after unmounting so the installed system can import it
    assert cmds.index("zpool export rpool") > cmds.index(f"umount --recursive {root}")


def test_zfs_native_encryption(target):
    root, _ = target
    (root / "lib/modules/6.12.40-gentoo-dist").mkdir(parents=True)
    cfg = make_config(filesystem="zfs", encrypt=True, luks_passphrase="zfs passphrase")
    runner, _, root = run_install(cfg, target)
    i, create = next((i, c) for i, c in enumerate(runner.commands) if c[:2] == ["zpool", "create"])
    assert "encryption=on" in create and "keylocation=prompt" in create
    assert runner.inputs[i] == "zfs passphrase\n"
    assert not any(c[0] == "cryptsetup" for c in runner.commands)  # no LUKS below ZFS
    grub = (root / "etc/default/grub").read_text()
    assert 'GRUB_CMDLINE_LINUX="rd.vconsole.keymap=de"' in grub
    assert not any("zfs passphrase" in line for line in runner.lines)


def test_zfs_without_kernel_fails(target):
    with pytest.raises(backend.InstallError, match="No kernel"):
        run_install(make_config(filesystem="zfs"), target)


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


@pytest.mark.parametrize("overrides", [
    {"encrypt": True, "luks_passphrase": "short"},
    {"encrypt": True, "luks_passphrase": "long enough", "mode": "manual",
     "root_partition": "/dev/sda3", "efi_partition": "/dev/sda1"},
    {"filesystem": "zfs", "mode": "manual", "root_partition": "/dev/sda3", "efi_partition": "/dev/sda1"},
    {"filesystem": "xfs"},
])
def test_invalid_disk_setup_is_rejected(target, overrides):
    root, listing = target
    runner = FakeRunner()
    inst = backend.Installer(make_config(**overrides), runner, edition=EDITION,
                             target=str(root), live_files=str(listing))
    with pytest.raises(backend.InstallError):
        inst.run()
    assert not any(c[0] in ("wipefs", "sfdisk", "cryptsetup", "zpool") for c in runner.commands)


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
    assert backend.validate_passphrase("12345678", "12345678") is None
    assert backend.validate_passphrase("1234567", "1234567")
    assert backend.validate_passphrase("12345678", "12345679")
    assert backend.suggest_username("Sam Smith") == "sam"
    assert backend.suggest_username("42 Things") == "u42"


V3 = "fpu lm sse sse2 ssse3 sse4_1 sse4_2 avx avx2 fma bmi1 bmi2 movbe f16c aes pclmulqdq"
FLAGS = {
    "zenplus": V3 + " sha_ni sse4a",
    "zen3": V3 + " sha_ni sse4a vaes vpclmulqdq",
    "zen4": V3 + " sha_ni sse4a vaes vpclmulqdq avx512f avx512bw avx512vl avx512_bf16 avx512_vnni",
    "zen5": V3 + " sha_ni sse4a vaes vpclmulqdq avx512f avx512bw avx512vl avx512_bf16 avx512_vnni"
            " avx512_vp2intersect avx_vnni movdiri movdir64b",
}


def cpuinfo(vendor, family, model, flags, threads=2):
    block = f"vendor_id\t: {vendor}\ncpu family\t: {family}\nmodel name\t: {model}\nflags\t\t: {flags}\n"
    return "".join(f"processor\t: {i}\n{block}\n" for i in range(threads))


def load_edition(cpu_id):
    """Read the real config/cpu/<id>.conf the build uses."""
    conf = system.read_edition(os.path.join(os.path.dirname(__file__), "../../config/cpu", f"{cpu_id}.conf"))
    assert conf["CPU_ID"] == cpu_id
    return conf


CPUS = {
    "2600X": system.parse_cpuinfo(cpuinfo("AuthenticAMD", 23, "AMD Ryzen 5 2600X", FLAGS["zenplus"])),
    "5950X": system.parse_cpuinfo(cpuinfo("AuthenticAMD", 25, "AMD Ryzen 9 5950X", FLAGS["zen3"])),
    "7950X": system.parse_cpuinfo(cpuinfo("AuthenticAMD", 25, "AMD Ryzen 9 7950X", FLAGS["zen4"])),
    "9950X": system.parse_cpuinfo(cpuinfo("AuthenticAMD", 26, "AMD Ryzen 9 9950X", FLAGS["zen5"])),
    "i5-10400": system.parse_cpuinfo(cpuinfo("GenuineIntel", 6, "Intel Core i5-10400", V3)),
    "i7-14700K": system.parse_cpuinfo(cpuinfo("GenuineIntel", 6, "Intel Core i7-14700K", V3 + " sha_ni vaes avx_vnni")),
    "G6400": system.parse_cpuinfo(cpuinfo("GenuineIntel", 6, "Intel Pentium Gold G6400", "fpu lm sse sse2 sse4_2 aes")),
    # QEMU's default CPU model on a Ryzen host: no AVX2, "hypervisor" flag set
    "qemu64": system.parse_cpuinfo(cpuinfo("AuthenticAMD", 15, "QEMU Virtual CPU version 2.5+",
                                           "fpu lm sse sse2 hypervisor")),
}


def test_parse_cpuinfo():
    cpu = CPUS["2600X"]
    assert (cpu.family, cpu.threads, cpu.vendor) == (23, 2, "AuthenticAMD")
    assert "sse4a" in cpu.flags


@pytest.mark.parametrize("name,best", [
    ("2600X", "zenplus"), ("5950X", "zen3"), ("7950X", "zen4"), ("9950X", "zen5"),
    ("i5-10400", "intel"), ("i7-14700K", "intel"), ("G6400", "generic"), ("qemu64", "generic"),
])
def test_recommended_edition(name, best):
    assert system.recommended_cpu_edition(CPUS[name]) == best


@pytest.mark.parametrize("name", ["2600X", "5950X", "7950X", "9950X", "i5-10400", "i7-14700K", "G6400", "qemu64"])
def test_matching_edition_has_no_warning(name):
    best = system.recommended_cpu_edition(CPUS[name])
    assert system.cpu_compatibility(load_edition(best), CPUS[name]) is None


@pytest.mark.parametrize("edition,cpu", [
    ("zen3", "2600X"), ("zen4", "5950X"), ("zen5", "7950X"), ("zenplus", "i7-14700K"),
    ("zen3", "i5-10400"), ("intel", "G6400"),
])
def test_too_new_edition_is_an_error(edition, cpu):
    severity, message = system.cpu_compatibility(load_edition(edition), CPUS[cpu])
    assert severity == "error" and "Illegal instruction" in message


@pytest.mark.parametrize("edition,cpu", [
    ("zenplus", "9950X"), ("zen3", "7950X"), ("intel", "5950X"), ("intel", "9950X"),
    ("generic", "7950X"), ("generic", "i7-14700K"),
])
def test_older_edition_works_with_a_hint(edition, cpu):
    severity, message = system.cpu_compatibility(load_edition(edition), CPUS[cpu])
    assert severity == "warning" and "tuned for it" in message


def test_vm_without_cpu_passthrough_gets_a_hint():
    severity, message = system.cpu_compatibility(load_edition("zen4"), CPUS["qemu64"])
    assert severity == "error"
    assert "host-passthrough" in message and "generic" in message
    # real hardware gets no VM hint
    assert "host-passthrough" not in system.cpu_compatibility(load_edition("zen4"), CPUS["2600X"])[1]


@pytest.mark.parametrize("vendor,product,xen,flags,expected", [
    ("QEMU", "Standard PC (Q35 + ICH9, 2009)", False, "", "qemu"),
    ("Red Hat", "KVM", False, "", "qemu"),
    ("VMware, Inc.", "VMware20,1", False, "", "vmware"),
    ("Microsoft Corporation", "Virtual Machine", False, "", "hyperv"),
    ("innotek GmbH", "VirtualBox", False, "", "virtualbox"),
    ("Xen", "HVM domU", False, "", "xen"),
    ("", "", True, "", "xen"),
    ("ASUSTeK COMPUTER INC.", "System Product Name", False, "", "none"),
    ("Microsoft Corporation", "Surface Laptop 5", False, "", "none"),
    ("Micro-Star International Co., Ltd.", "MS-7C56", False, "hypervisor", "other"),
])
def test_detect_hypervisor(tmp_path, vendor, product, xen, flags, expected):
    dmi = tmp_path / "class/dmi/id"
    dmi.mkdir(parents=True)
    (dmi / "sys_vendor").write_text(vendor + "\n")
    (dmi / "product_name").write_text(product + "\n")
    if xen:
        (tmp_path / "hypervisor").mkdir()
        (tmp_path / "hypervisor/type").write_text("xen\n")
    cpu = system.CpuInfo(flags=frozenset(flags.split()))
    assert system.detect_hypervisor(str(tmp_path), cpu=cpu) == expected


def test_unknown_cpu_or_edition_is_not_judged():
    assert system.cpu_compatibility({}, CPUS["2600X"]) is None
    assert system.cpu_compatibility(load_edition("zen5"), system.CpuInfo()) is None


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
    f.write_text('DISTRO_NAME="Gentoo Linux"\nEDITION="zen3-nvidia"\nCPU_REQUIRED_FLAGS="avx2 vaes"\n')
    ed = system.read_edition(str(f))
    assert ed["DISTRO_NAME"] == "Gentoo Linux" and ed["CPU_REQUIRED_FLAGS"] == "avx2 vaes"
    assert system.read_edition(str(tmp_path / "nope"))["DISTRO_ID"] == "gentoo"
