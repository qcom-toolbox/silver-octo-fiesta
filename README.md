# Gentoo Linux for AMD Ryzen AM4

This repository builds a Gentoo Linux live and installer ISO for AMD Ryzen
AM4 desktops. The whole system is compiled for your CPU generation. It boots
straight into a full Wayland KDE Plasma desktop and runs OpenRC (no systemd).
A graphical installer puts it on your disk in a few clicks.

| | |
|---|---|
| **Base** | Gentoo `default/linux/amd64/23.0/desktop/plasma` profile, OpenRC, stage3 `desktop-openrc` |
| **Desktop** | KDE Plasma 6 on Wayland, SDDM with a Wayland greeter (no Xorg needed) |
| **Kernel** | `sys-kernel/gentoo-kernel` plus an AM4 config fragment (amd-pstate, 1000 Hz, full preemption, k10temp) |
| **Graphics** | NVIDIA RTX 3000/4000/5000 (open kernel modules), or AMD Radeon / Ryzen APU (mesa) |
| **Tools** | sudo, neofetch, fastfetch, hyfetch, screenfetch, htop, btop, atop, KDE Partition Manager, Konsole, Dolphin, Kate… |
| **Installer** | Graphical Qt 6 wizard with automatic or manual (dual-boot) partitioning, Btrfs or ext4, UEFI or BIOS |

> For now the distribution is branded **Gentoo Linux**. To rename it, see
> [Rebranding](#rebranding). Everything reads the name from one file.

## Which ISO do I need?

There are two CPU editions. Code compiled for Zen 3 uses instructions such as
VAES and VPCLMULQDQ, which a Zen+ CPU does not have, so one image cannot be
tuned for both. Each CPU edition comes with two graphics flavours.

| Your CPU | CPU edition | `-march` |
|---|---|---|
| Ryzen 5 **2600X** (and every Ryzen 1000/2000/3000) | `zenplus` | `znver1` |
| Ryzen 7 **5700G**, Ryzen 9 **5950X** (every Ryzen 5000) | `zen3` | `znver3` |

| Your graphics card | GPU flavour |
|---|---|
| GeForce **RTX 3000 / 4000 / 5000** | `nvidia`: NVIDIA open kernel modules, which RTX 5000 requires. The amdgpu driver is included too, so the 5700G's iGPU also works |
| Radeon card, or only the 5700G's integrated graphics | `amd`: mesa only, smaller image |

This gives four ISOs, for example:

- `gentoo-am4-zenplus-nvidia-YYYYMMDD.iso` for a 2600X with an RTX 3070
- `gentoo-am4-zen3-nvidia-YYYYMMDD.iso` for a 5950X with an RTX 5080, or a 5700G with an RTX 4070
- `gentoo-am4-zen3-amd-YYYYMMDD.iso` for a 5700G using only its iGPU

The installer checks the CPU. If you boot the wrong edition, for example
`zen3` on a 2600X, it warns you before installing.

## Building an ISO

### Requirements

- A Linux x86_64 machine with root access. Any distribution works.
- **The build machine's CPU must be able to run the edition's code.** Build
  `zen3` on a Ryzen 5000 or newer. `zenplus` builds on any Zen+ or newer
  machine. `build.sh` checks this for you.
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

# Every combination (4 ISOs)
sudo ./build.sh --cpu all --gpu all
```

The ISO and its `.sha256` file end up in `out/`. The build runs in three
steps, and you can run them separately with `--step`:

| Step | What happens |
|---|---|
| `fetch` | Downloads the latest `stage3-amd64-desktop-openrc`, checks its SHA256 and GPG signature, and extracts it to `work/<edition>/rootfs` |
| `build` | Inside the chroot: sets up Portage and the Plasma profile, recompiles `@world` with `-march=znver1/znver3`, then installs the kernel, drivers, Plasma, the applications and the installer, configures OpenRC services and creates the live user |
| `iso` | Builds a dracut `dmsquash-live` initramfs, compresses the root filesystem with squashfs (zstd), and runs `grub-mkrescue` to make a hybrid BIOS/UEFI ISO |

Compiling everything from source takes many hours: roughly 6–10 h on a 5950X
and a lot more on a 2600X. Useful options:

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
sudo dd if=out/gentoo-am4-zen3-nvidia-*.iso of=/dev/sdX bs=4M status=progress oflag=sync
```

The ISO also works with Ventoy, Fedora Media Writer and similar tools. It
boots in UEFI and in legacy BIOS/CSM mode.

**Disable Secure Boot.** The distribution kernel and the NVIDIA modules are
not signed with Microsoft's keys.

## Using the live system

The GRUB menu offers:

- **Live**: the normal boot option.
- **Copy to RAM**: after booting you can remove the USB stick.
- **Basic graphics**: disables the NVIDIA and amdgpu drivers. Use it if the screen stays black.
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
  - File system: **Btrfs** (zstd compression, `@` and `@home` subvolumes) or **ext4**.
- **User** asks for your name, user name, computer name and password. Two
  options: use the same password for root, and log in automatically.
- **Install** copies the system with rsync, writes `fstab`, locale, time zone,
  keyboard, user and SDDM settings, removes the live-session files, sets
  `MAKEOPTS` to match your cores and RAM, and installs GRUB. If it finds other
  operating systems with os-prober, it adds them to the boot menu.

The log is kept at `/var/log/am4-installer.log`.

To try the installer without touching any disk, even on a non-Gentoo machine
with PySide6 installed, run:

```sh
cd installer && python3 -m am4_installer --dry-run
```

## After installing

- `am4-update`: syncs Portage, updates `@world` and `--depclean`s, runs
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
- The installed `/etc/portage/make.conf` keeps the same `-march` and
  `CPU_FLAGS_X86`. Everything you compile later is tuned for your CPU too.

## Repository layout

```
build.sh                    host-side driver: fetch → build → iso
config/
  distro.conf               name, profile, stage3 flavour  (rebranding happens here)
  cpu/{zenplus,zen3}.conf   -march, CPU_FLAGS_X86, compatibility checks
  gpu/{nvidia,amd}/         VIDEO_CARDS, driver USE flags/licenses, extra files
  portage/                  make.conf template, package.use/license/keywords, @am4-core set
  packages/extras.list      desktop applications (with fallbacks for renamed packages)
  kernel/am4.config         kernel config fragment (/etc/kernel/config.d)
rootfs/                     files copied into every image (SDDM Wayland, OpenRC, sysctl, ...)
rootfs-live/                files only for the live session (removed by the installer)
iso/grub.cfg.in             live ISO boot menu
scripts/chroot/             build-system.sh and make-iso.sh, run inside the chroot
installer/                  the Qt 6 installer (am4_installer package, launcher, tests)
```

## Customising

- **More packages:** add them to `config/packages/extras.list`. Write
  `a | b` to use whichever of the two exists in the tree. Packages that cannot
  be installed are skipped with a warning. The packages in `@am4-core` are
  different: if one of them fails, the build fails.
- **USE flags:** edit `config/portage/package.use/00-am4-desktop` or the `USE=` line in `config/portage/make.conf.in`.
- **Another CPU generation:** copy `config/cpu/zen3.conf`, for example to
  `zen4.conf` with `znver4` and your `cpuid2cpuflags` output, and build with `--cpu zen4`.

### Rebranding

Change `DISTRO_NAME`, `DISTRO_SHORT` and `DISTRO_ID` in `config/distro.conf`.
These values flow into the ISO name and label, the GRUB menu, the installer,
the desktop entries and the GRUB distributor. If the name is no longer
"Gentoo Linux", the build also writes a matching `/etc/os-release`.

## Development

```sh
shellcheck build.sh scripts/lib.sh scripts/chroot/*.sh installer/am4-installer
cd installer && python3 -m pytest -q tests
```

CI (`.github/workflows/lint.yml`) runs shellcheck and the installer's unit
tests. A full ISO build takes far longer than a hosted CI runner allows, so
build ISOs on a real AM4 machine.
