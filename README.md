# Gentoo Linux desktop

This repository builds a Gentoo Linux live and installer ISO for AMD Ryzen
(AM4 and AM5) and Intel Core desktops, with NVIDIA, AMD or Intel graphics.
The whole system is compiled for your CPU generation. It boots
straight into a full Wayland KDE Plasma desktop and runs OpenRC (no systemd).
A graphical installer puts it on your disk in a few clicks.

| | |
|---|---|
| **Base** | Gentoo `default/linux/amd64/23.0/desktop/plasma` profile, OpenRC, stage3 `desktop-openrc` |
| **Desktop** | KDE Plasma 6 on Wayland, SDDM with a Wayland greeter (no Xorg needed) |
| **Kernel** | `sys-kernel/gentoo-kernel` plus a desktop config fragment (amd-pstate/intel_pstate, 1000 Hz, full preemption, i915 + xe for Intel graphics) |
| **CPUs** | AMD Ryzen 1000–9000, Intel Core 10th gen (2020) and newer, Core Ultra |
| **Graphics** | NVIDIA RTX 3000/4000/5000 (open kernel modules), or AMD Radeon RX 6000–9000 / Intel Arc / integrated graphics (mesa) |
| **Tools** | sudo, neofetch, fastfetch, hyfetch, screenfetch, htop, btop, atop, KDE Partition Manager, Konsole, Dolphin, Kate… |
| **Installer** | Graphical Qt 6 wizard: automatic or manual (dual-boot) partitioning, Btrfs, ext4 or ZFS, automatic snapshots (Snapper / ZFS), optional encryption (LUKS2 / native ZFS), UEFI or BIOS |

> The distribution is called **Gentoo Linux**. It uses Gentoo's own
> `/etc/os-release`, so neofetch and other tools show it as Gentoo.

## Which ISO do I need?

Pick one **CPU edition** and one **graphics edition**. Each CPU edition is
compiled for its generation. A newer generation's code uses instructions
(AVX-512, VAES, ...) that older CPUs lack, so one image cannot be tuned for all.

| Your CPU | CPU edition | Compiled with |
|---|---|---|
| AMD Ryzen 1000 / 2000 / 3000 (e.g. **2600X**, 3700X) | `zenplus` | `-march=znver1` |
| AMD Ryzen 5000 (e.g. **5700G**, **5950X**, 5800X3D) | `zen3` | `-march=znver3` |
| AMD Ryzen 7000, AM5 (e.g. 7600X, **7800X3D**, **7950X**) | `zen4` | `-march=znver4` |
| AMD Ryzen 9000, AM5 (e.g. 9700X, **9800X3D**, **9950X**) | `zen5` | `-march=znver5` |
| Intel Core 10th–14th gen, Core Ultra 100/200 (2020 and newer) | `intel` | `-march=x86-64-v3 -mtune=intel` |

Intel CPUs share one edition. Their generations differ in instruction sets
(12th gen and newer, for example, have no AVX-512), so the edition targets
the AVX2 level all 2020+ Core CPUs have. It also includes the Intel microcode
package. Pentium and Celeron models without AVX2 are not supported.

| Your graphics | Graphics edition |
|---|---|
| NVIDIA GeForce **RTX 3000 / 4000 / 5000** | `nvidia`: NVIDIA open kernel modules, which RTX 5000 requires. AMD and Intel integrated graphics also work |
| AMD Radeon **RX 6000 / 7000 / 9000**, Intel **Arc** A/B series, or only integrated graphics (Ryzen APU, Intel UHD / Iris Xe) | `mesa`: open-source drivers only, smaller image |

Examples:

- `gentoo-desktop-zenplus-nvidia-YYYYMMDD.iso` for a Ryzen 5 2600X with an RTX 3070
- `gentoo-desktop-zen3-nvidia-YYYYMMDD.iso` for a Ryzen 9 5950X with an RTX 5080
- `gentoo-desktop-zen3-mesa-YYYYMMDD.iso` for a Ryzen 7 5700G using only its iGPU
- `gentoo-desktop-zen4-mesa-YYYYMMDD.iso` for a Ryzen 7 7800X3D with a Radeon RX 7900 XTX
- `gentoo-desktop-zen5-nvidia-YYYYMMDD.iso` for a Ryzen 9 9950X with an RTX 4090
- `gentoo-desktop-intel-mesa-YYYYMMDD.iso` for a Core i5-12400 with an Intel Arc B580

The installer checks the CPU. If you boot an edition that is too new for your
processor, for example `zen4` on a 2600X, it warns you before installing and
names the edition to use. If an older edition runs on your CPU but a better one
exists, it tells you that too.

## Building an ISO

### Requirements

- A Linux x86_64 machine with root access. Any distribution works.
- **The build machine's CPU must be able to run the edition's code.**
  `build.sh` checks this for you:
  - `zen3`: build on a Ryzen 5000 or newer.
  - `zen4`: build on a Ryzen 7000 or newer.
  - `zen5`: build on a Ryzen 9000.
  - `zenplus`: builds on any AMD Ryzen.
  - `intel`: builds on any 2020+ Intel Core or on any Ryzen.
- About 60 GB of free disk space per edition, plus about 20 GB for the shared caches.
- 16 GB of RAM or more is recommended.
- Host tools: `bash curl tar xz sha256sum chroot mount`. `gpg` is recommended
  so the stage3 signature is verified.

Nothing else is needed on the host. The squashfs, GRUB and ISO tools are
installed and run inside the Gentoo chroot.

### Build

```sh
git clone <this repo> && cd <this repo>

# Ryzen 5000 (5700G / 5950X) with a GeForce RTX card
sudo ./build.sh --cpu zen3 --gpu nvidia

# Ryzen 2600X with a GeForce RTX card
sudo ./build.sh --cpu zenplus --gpu nvidia

# Ryzen 9000 with a Radeon card
sudo ./build.sh --cpu zen5 --gpu mesa

# Intel Core (2020+) with Intel Arc or integrated graphics
sudo ./build.sh --cpu intel --gpu mesa

# Every combination (10 ISOs)
sudo ./build.sh --cpu all --gpu all
```

The ISO and its `.sha256` file end up in `out/`. The build runs in three
steps, and you can run them separately with `--step`:

| Step | What happens |
|---|---|
| `fetch` | Downloads the latest `stage3-amd64-desktop-openrc`, checks its SHA256 and GPG signature, and extracts it to `work/<edition>/rootfs` |
| `build` | Inside the chroot: sets up Portage and the Plasma profile, recompiles `@world` with the edition's `-march`, then installs the kernel, drivers, Plasma, the applications and the installer, configures OpenRC services and creates the live user |
| `iso` | Builds a dracut `dmsquash-live` initramfs, compresses the root filesystem with squashfs (zstd), and runs `grub-mkrescue` to make a hybrid BIOS/UEFI ISO |

Compiling everything from source takes many hours: roughly 5–10 h on a 5950X,
7950X or 9950X, and a lot more on a 6-core CPU such as the 2600X. Useful options:

- `--binhost` uses Gentoo's official **x86-64-v3** binary packages wherever
  they match. The build is much faster, but those packages are tuned less
  specifically for your CPU.
- `--no-rebuild` does not recompile the stage3's existing packages. Only
  newly installed packages get `-march` tuning.
- Every compiled package is cached in `work/cache/binpkgs/<edition>`. If you
  re-run a build that failed or was interrupted, it continues where it
  stopped instead of starting over.
- `--clean` deletes an edition's root filesystem and keeps the caches.

### Write the ISO to a USB stick

```sh
sudo dd if=out/gentoo-desktop-zen3-nvidia-*.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The ISO also works with Ventoy, Fedora Media Writer and similar tools. It
boots in UEFI and in legacy BIOS/CSM mode.

**Disable Secure Boot.** The distribution kernel and the NVIDIA modules are
not signed with Microsoft's keys.

## Using the live system

The GRUB menu offers:

- **Live**: the normal boot option.
- **Copy to RAM**: after booting you can remove the USB stick.
- **Basic graphics**: disables the NVIDIA, AMD and Intel graphics drivers. Use it if the screen stays black.
- **Verbose boot**: for troubleshooting.

The live session logs in automatically as `live` (no password, passwordless
`sudo`). The installer starts on its own. You can also open it later from the
**Install Gentoo Linux** icon on the desktop or in the application menu.

## The installer

The installer has these steps: **Welcome → Location → Disk → User → Summary → Install → Finish**.

- **Welcome** shows your CPU, GPU, RAM and boot mode, and warns you if the
  edition does not match your hardware. You also choose the language here.
- **Location** asks for the time zone and keyboard layout. Both lists are searchable.
- **Disk** has two modes:
  - *Erase a disk*: creates a GPT with a 1 GiB EFI partition and a root
    partition. On a BIOS machine the first partition is a BIOS boot partition instead.
  - *Use existing partitions*: for dual booting next to Windows. Pick a root
    partition and the existing EFI partition, which is kept by default. You
    can open KDE Partition Manager from this page to make room first.
  - File system: **Btrfs**, **ext4** or **ZFS**. Btrfs uses zstd compression and these subvolumes:

    | Subvolume | Mounted at | Why |
    |---|---|---|
    | `@` | `/` | the system |
    | `@home` | `/home` | your files, kept out of system snapshots |
    | `@snapshots` | `/.snapshots` | a place for snapshots of `@` |
    | `@log` | `/var/log` | logs survive rolling back the system |
    | `@cache` | `/var/cache` | Portage downloads and caches don't bloat snapshots |

  - **ZFS** (only when erasing a whole disk) creates the pool `rpool` with
    zstd compression and the datasets `rpool/ROOT/gentoo` (`/`),
    `rpool/home`, `rpool/var/log` and `rpool/var/cache`. GRUB boots from a
    small ext4 `/boot` partition, and the initramfs imports the pool. ZFS is
    only offered if the image could include it: `sys-fs/zfs-kmod` must
    support the kernel version, and Portage keeps the kernel within that
    range. OpenZFS is CDDL-licensed. Consider that if you redistribute ISOs
    that contain the ZFS kernel module.
  - **Automatic snapshots** (Btrfs and ZFS, on by default):
    - Btrfs uses **Snapper** with the config `root`.
    - ZFS uses `zfs snapshot` on `rpool/ROOT/gentoo` and `rpool/home`.
    - A snapshot is taken every hour and every day by cron, plus one before
      and one after every `gentoo-update`.
    - Old snapshots are removed automatically. Snapper keeps 10 hourly,
      7 daily, 4 weekly and 3 monthly snapshots plus the last 10 update
      pairs. ZFS keeps 24 hourly, 7 daily and 10 update snapshots.

    Run `sudo gentoo-snapshot list` to see them. To undo an update on Btrfs, run
    `sudo snapper -c root undochange <pre>..<post>`. On ZFS, run
    `sudo zfs rollback -r rpool/ROOT/gentoo@<snapshot>`.
  - **Encryption:** tick *Encrypt the system* and choose a passphrase of at
    least 8 characters. The disk then gets an EFI partition, an unencrypted
    1 GiB `/boot` (kernel and initramfs only), and one encrypted partition
    holding everything else. Btrfs and ext4 use LUKS2; ZFS uses its native
    encryption. At every boot the initramfs asks for the passphrase using
    your keyboard layout. It works in both UEFI and BIOS mode. It is only offered when erasing a whole
    disk. If you forget the passphrase, the data cannot be recovered.
- **User** asks for your name, user name, computer name and password. Two
  options: use the same password for root, and log in automatically.
- **Install** copies the system with rsync, writes `fstab`, locale, time zone,
  keyboard, user and SDDM settings, removes the live-session files, sets
  `MAKEOPTS` to match your cores and RAM, and installs GRUB. If it finds other
  operating systems with os-prober, it adds them to the boot menu.

The log is kept at `/var/log/gentoo-installer.log`.

To try the installer without touching any disk, even on a non-Gentoo machine
with PySide6 installed, run:

```sh
cd installer && python3 -m gentoo_installer --dry-run
```

## After installing

- `gentoo-update`: syncs Portage, updates `@world` and `--depclean`s, runs
  `dispatch-conf` and updates Flatpak apps.
- **Discover** installs Flatpak apps from Flathub, such as Steam, Discord or
  OBS. The system already has `vm.max_map_count` raised for games.
- The user you create in the installer is in the `wheel` group and can run
  anything with `sudo`. Root logins stay locked unless you tick "same password for root".
- `neofetch` greets you in every new Konsole window. `fastfetch`, `hyfetch`,
  `screenfetch`, `htop`, `btop` and `atop` are installed too.
- zram swap (half of your RAM, zstd) is enabled through `zram-init`.
- NVIDIA systems are preconfigured: `nvidia_drm.modeset=1 fbdev=1`, early
  loading in the initramfs, nouveau blacklisted, and an elogind sleep hook so
  suspend and resume work on OpenRC.
- Intel systems get early-loaded Intel microcode. AMD microcode comes with
  `linux-firmware`. Video decoding works on AMD (mesa), Intel
  (`intel-media-driver`) and NVIDIA (`nvidia-vaapi-driver`).
- The installed `/etc/portage/make.conf` keeps the same `-march` and
  `CPU_FLAGS_X86`. Everything you compile later is tuned for your CPU too.

## Repository layout

```
build.sh                    host-side driver: fetch → build → iso
config/
  distro.conf               name (Gentoo Linux), profile, stage3 flavour
  cpu/*.conf                zenplus, zen3, zen4, zen5, intel: -march, CPU_FLAGS_X86, required CPU flags
  gpu/{nvidia,mesa}/        VIDEO_CARDS, driver USE flags/licenses, extra packages and files
  portage/                  make.conf template, package.use/license/keywords, @desktop-core set
  packages/extras.list      desktop applications (with fallbacks for renamed packages)
  kernel/desktop.config     kernel config fragment (/etc/kernel/config.d)
rootfs/                     files copied into every image (SDDM Wayland, OpenRC, sysctl, ...)
rootfs-live/                files only for the live session (removed by the installer)
iso/grub.cfg.in             live ISO boot menu
scripts/chroot/             build-system.sh and make-iso.sh, run inside the chroot
installer/                  the Qt 6 installer (gentoo_installer package, launcher, tests)
```

## Customising

- **More packages:** add them to `config/packages/extras.list`. Write
  `a | b` to use whichever of the two exists in the tree. Packages that cannot
  be installed are skipped with a warning. The packages in `@desktop-core` are
  different: if one of them fails, the build fails.
- **USE flags:** edit `config/portage/package.use/00-desktop` or the `USE=` line in `config/portage/make.conf.in`.
- **Another CPU generation:** copy a file in `config/cpu/`, for example to
  `zen6.conf`. Set `CPU_CFLAGS`, your `cpuid2cpuflags` output and the
  `/proc/cpuinfo` flags it requires, then build with `--cpu zen6`. The installer
  picks up `CPU_REQUIRED_FLAGS` automatically.

## Development

```sh
shellcheck build.sh scripts/lib.sh scripts/chroot/*.sh installer/gentoo-installer
cd installer && python3 -m pytest -q tests
```

CI (`.github/workflows/lint.yml`) runs shellcheck and the installer's unit
tests. A full ISO build takes far longer than a hosted CI runner allows, so
build ISOs on a real desktop machine.
