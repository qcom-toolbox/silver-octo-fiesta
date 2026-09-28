# Gentoo Linux live ISO: generic-mesa (2026-09-27)

This branch only holds the ISO, split into 185 pieces of 30 MiB
(`gentoo-desktop-generic-mesa-20260927.iso.000` to `.184`), because GitHub does not accept big files.
The source code is on the `claude/epic-lamport-kv095b` branch.

The `generic-mesa` edition runs on any x86-64 CPU and supports AMD, Intel and
VM graphics. It has KDE Plasma (Wayland), SDDM, OpenRC and the graphical
installer. Built with `./build.sh --cpu generic --gpu mesa --binhost`.

## Download

Clone only this branch (about 5.8 GB):

```sh
git clone --single-branch --depth 1 -b claude/iso-generic-mesa-20260927 https://github.com/qcom-toolbox/silver-octo-fiesta gentoo-iso
```

## Join the pieces

- **Windows:** double-click `join.bat`
- **Linux / macOS:** `./join.sh`

Both create `gentoo-desktop-generic-mesa-20260927.iso` and check its SHA-256:

```
3c2eda9276dd5ade4a044fa08aeaf6d05d4903f2407642ebb0e2858f31f832d8
```

Then write the ISO to a USB stick (at least 8 GB) with Rufus (DD mode),
balenaEtcher or `dd`, or boot it in a VM with at least 8 GB of RAM.
Live user: `live`, no password. Run **Install Gentoo Linux** from the desktop.
