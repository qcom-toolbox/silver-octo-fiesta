#!/bin/bash
# Build a Gentoo Linux live/installer ISO optimised for one CPU generation
# (AMD Ryzen or Intel Core, 2017-2026) and one graphics stack.
#
#   sudo ./build.sh --cpu zen3 --gpu nvidia
#
# See README.md for the full documentation.
set -euo pipefail

TOP=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
# shellcheck source=scripts/lib.sh
source "${TOP}/scripts/lib.sh"
# shellcheck source=config/distro.conf
source "${TOP}/config/distro.conf"

usage() {
	cat <<EOF
Usage: sudo $0 --cpu <edition> [options]

Editions:
  -c, --cpu <edition|all>       CPU the image is compiled for
                                  zenplus: Ryzen 1000-3000 (e.g. Ryzen 5 2600X)
                                  zen3:    Ryzen 5000 (e.g. Ryzen 7 5700G, Ryzen 9 5950X)
                                  zen4:    Ryzen 7000 (e.g. Ryzen 7 7800X3D, Ryzen 9 7950X)
                                  zen5:    Ryzen 9000 (e.g. Ryzen 7 9800X3D, Ryzen 9 9950X)
                                  intel:   Intel Core 10th gen and newer, Core Ultra
  -g, --gpu <nvidia|mesa|all>   Graphics stack (default: nvidia)
                                  nvidia:  GeForce RTX 3000/4000/5000 (+ AMD/Intel iGPU)
                                  mesa:    AMD Radeon RX 6000-9000, Intel Arc / Iris Xe,
                                           integrated graphics (aliases: amd, intel)

Options:
  -s, --step <list>     Comma separated steps to run: fetch,build,iso (default: all)
  -j, --jobs <n>        Parallel compile jobs (default: $(nproc))
      --binhost         Use Gentoo's official x86-64-v3 binary packages where
                        possible (much faster build, less CPU-specific tuning)
      --no-rebuild      Do not recompile the stage3 packages (@world) for the
                        selected CPU, only newly installed packages are tuned
      --work <dir>      Work directory (default: ./work)
      --out <dir>       Output directory for ISOs (default: ./out)
      --no-gpg          Skip the GPG signature check of the stage3 tarball
      --force           Build even if this machine cannot run the edition's code
      --clean           Delete the edition's root filesystem (caches are kept) and exit
  -h, --help            Show this help
EOF
}

CPU="" GPU="nvidia" JOBS=$(nproc) WORK="${TOP}/work" OUT="${TOP}/out"
STEPS="fetch,build,iso" BINHOST=0 REBUILD=1 FORCE=0 CLEAN=0 NO_GPG=0
ORIG_ARGS=("$@")

while [[ $# -gt 0 ]]; do
	case $1 in
		-c | --cpu) CPU=${2:?}; shift ;;
		-g | --gpu) GPU=${2:?}; shift ;;
		-s | --step | --steps) STEPS=${2:?}; shift ;;
		-j | --jobs) JOBS=${2:?}; shift ;;
		--work) WORK=$(realpath -m "${2:?}"); shift ;;
		--out) OUT=$(realpath -m "${2:?}"); shift ;;
		--binhost) BINHOST=1 ;;
		--no-rebuild) REBUILD=0 ;;
		--no-gpg) NO_GPG=1 ;;
		--force) FORCE=1 ;;
		--clean) CLEAN=1 ;;
		-h | --help) usage; exit 0 ;;
		*) usage >&2; die "Unknown option: $1" ;;
	esac
	shift
done

[[ -n ${CPU} ]] || { usage >&2; die "--cpu is required"; }

# "all" builds every combination by re-running this script once per edition.
if [[ ${CPU} == all || ${GPU} == all ]]; then
	cpus=("${CPU}") gpus=("${GPU}")
	[[ ${CPU} == all ]] && cpus=(zenplus zen3 zen4 zen5 intel)
	[[ ${GPU} == all ]] && gpus=(nvidia mesa)
	# Drop the --cpu/--gpu values from the original arguments; they are replaced below.
	args=() skip=0
	for a in "${ORIG_ARGS[@]}"; do
		if ((skip)); then skip=0; continue; fi
		case ${a} in -c | --cpu | -g | --gpu) skip=1; continue ;; esac
		args+=("${a}")
	done
	for c in "${cpus[@]}"; do
		for g in "${gpus[@]}"; do
			"$0" "${args[@]}" --cpu "${c}" --gpu "${g}"
		done
	done
	exit 0
fi

# AMD and Intel graphics share the open "mesa" edition.
[[ ${GPU} == amd || ${GPU} == intel ]] && GPU=mesa
[[ -f ${TOP}/config/cpu/${CPU}.conf ]] || die "Unknown CPU edition '${CPU}' (see config/cpu/)"
[[ -f ${TOP}/config/gpu/${GPU}/gpu.conf ]] || die "Unknown GPU edition '${GPU}' (see config/gpu/)"
[[ ${JOBS} =~ ^[1-9][0-9]*$ ]] || die "--jobs must be a positive number"
# shellcheck source=config/cpu/zen3.conf
source "${TOP}/config/cpu/${CPU}.conf"
# shellcheck source=config/gpu/nvidia/gpu.conf
source "${TOP}/config/gpu/${GPU}/gpu.conf"

EDITION="${CPU}-${GPU}"
EDITION_DIR="${WORK}/${EDITION}"
ROOT="${EDITION_DIR}/rootfs"
ISOTREE="${EDITION_DIR}/isotree"
CACHE="${WORK}/cache"
DISTFILES="${CACHE}/distfiles"
BINPKGS="${CACHE}/binpkgs/${EDITION}"
STAGE3_DIR="${CACHE}/stage3"
BUILD_DATE=$(date +%Y%m%d)
ISO_NAME="${DISTRO_ID}-${DISTRO_VARIANT}-${EDITION}-${BUILD_DATE}.iso"
ISO_LABEL="${DISTRO_ID^^}_${CPU^^}_${GPU^^}"
ISO_LABEL=${ISO_LABEL:0:32}

# Mount points inside the chroot, in mount order (unmounted in reverse).
MOUNTS=()

# --------------------------------------------------------------------------
# Host checks
# --------------------------------------------------------------------------
check_host() {
	[[ ${EUID} -eq 0 ]] || die "Run as root (the build uses chroot and mount)."
	[[ $(uname -m) == x86_64 ]] || die "The build host must be x86_64."
	local cmd
	for cmd in curl tar xz sha256sum chroot mount umount mountpoint findmnt realpath; do
		command -v "${cmd}" >/dev/null || die "Missing host tool: ${cmd}"
	done

	# Code compiled with "${CPU_CFLAGS}" is executed during the build
	# (configure checks, build tools), so the host CPU must support it.
	local flag missing=()
	for flag in ${CPU_REQUIRED_FLAGS}; do
		grep -qw -- "${flag}" /proc/cpuinfo || missing+=("${flag}")
	done
	if ((${#missing[@]})); then
		if ((FORCE)); then
			warn "Host CPU lacks ${missing[*]}: build may crash with 'Illegal instruction'."
		else
			die "This machine's CPU lacks ${missing[*]} and cannot run code built with ${CPU_CFLAGS}.
    Build the '${CPU}' edition on a ${CPU_DESC} (or newer) machine, or pass --force."
		fi
	fi
}

# --------------------------------------------------------------------------
# Chroot mounts
# --------------------------------------------------------------------------
bind_mount() { # <source> <target inside ROOT> [ro]
	local src=$1 dst="${ROOT}$2"
	mkdir -p "${src}" "${dst}"
	mount --bind "${src}" "${dst}"
	[[ ${3:-} == ro ]] && mount -o remount,bind,ro "${dst}"
	MOUNTS+=("${dst}")
}

mount_chroot() {
	((${#MOUNTS[@]} == 0)) || return 0
	info "Mounting pseudo filesystems and caches into ${ROOT}"
	mount --types proc /proc "${ROOT}/proc"
	MOUNTS+=("${ROOT}/proc")
	local fs
	for fs in sys dev; do
		mount --rbind "/${fs}" "${ROOT}/${fs}"
		mount --make-rslave "${ROOT}/${fs}"
		MOUNTS+=("${ROOT}/${fs}")
	done
	mount --bind /run "${ROOT}/run"
	mount --make-slave "${ROOT}/run"
	MOUNTS+=("${ROOT}/run")

	cp --dereference /etc/resolv.conf "${ROOT}/etc/resolv.conf"

	bind_mount "${DISTFILES}" /var/cache/distfiles
	bind_mount "${BINPKGS}" /var/cache/binpkgs
	bind_mount "${TOP}" /mnt/gentoo-src ro
	bind_mount "${OUT}" /mnt/gentoo-out
}

umount_chroot() {
	local i mp
	for ((i = ${#MOUNTS[@]} - 1; i >= 0; i--)); do
		mp=${MOUNTS[i]}
		if mountpoint -q "${mp}"; then
			umount -R "${mp}" 2>/dev/null || umount -R -l "${mp}" || warn "Could not unmount ${mp}"
		fi
	done
	MOUNTS=()
}
trap umount_chroot EXIT

run_chroot() {
	chroot "${ROOT}" /usr/bin/env -i \
		HOME=/root TERM="${TERM:-xterm}" LANG=C.UTF-8 \
		PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
		CPU="${CPU}" GPU="${GPU}" EDITION="${EDITION}" JOBS="${JOBS}" \
		BINHOST="${BINHOST}" REBUILD="${REBUILD}" BUILD_DATE="${BUILD_DATE}" \
		ISO_NAME="${ISO_NAME}" ISO_LABEL="${ISO_LABEL}" \
		/bin/bash "$@"
}

# --------------------------------------------------------------------------
# Steps
# --------------------------------------------------------------------------
step_fetch() {
	mkdir -p "${STAGE3_DIR}"
	info "Looking up the latest ${STAGE3_FLAVOUR}"
	local latest file
	latest=$(curl -fsSL "${STAGE3_MIRROR}/latest-${STAGE3_FLAVOUR}.txt" |
		grep -Eo "^[0-9]{8}T[0-9]{6}Z/${STAGE3_FLAVOUR}-[0-9TZ]+\.tar\.xz" | head -n1) ||
		die "Could not find the latest stage3 on ${STAGE3_MIRROR}"
	file=${latest##*/}

	if [[ ! -f ${STAGE3_DIR}/${file} ]]; then
		info "Downloading ${file}"
		curl -fL --retry 3 -o "${STAGE3_DIR}/${file}.part" "${STAGE3_MIRROR}/${latest}"
		mv "${STAGE3_DIR}/${file}.part" "${STAGE3_DIR}/${file}"
	fi
	curl -fsSL -o "${STAGE3_DIR}/${file}.sha256" "${STAGE3_MIRROR}/${latest}.sha256"

	info "Verifying SHA256 checksum"
	local expected actual
	expected=$(grep -Eo "^[0-9a-f]{64}[[:space:]]+${file}\$" "${STAGE3_DIR}/${file}.sha256" | awk '{print $1}')
	actual=$(sha256sum "${STAGE3_DIR}/${file}" | awk '{print $1}')
	if [[ -z ${expected} || ${expected} != "${actual}" ]]; then
		rm -f "${STAGE3_DIR}/${file}"
		die "Checksum mismatch for ${file} (deleted, run again)"
	fi

	if ((NO_GPG)); then
		warn "Skipping GPG verification (--no-gpg)"
	elif command -v gpg >/dev/null; then
		info "Verifying GPG signature (Gentoo release key via WKD)"
		curl -fsSL -o "${STAGE3_DIR}/${file}.asc" "${STAGE3_MIRROR}/${latest}.asc"
		local gnupg
		gnupg=$(mktemp -d)
		GNUPGHOME=${gnupg} gpg --quiet --auto-key-locate=clear,nodefault,wkd --locate-key releng@gentoo.org >/dev/null ||
			die "Could not fetch the Gentoo release key (use --no-gpg to skip)"
		GNUPGHOME=${gnupg} gpg --quiet --verify "${STAGE3_DIR}/${file}.asc" "${STAGE3_DIR}/${file}" ||
			die "Bad GPG signature on ${file}"
		rm -rf "${gnupg}"
	else
		warn "gpg not installed: only the checksum was verified"
	fi

	if [[ -f ${ROOT}/.gentoo-stage3 ]]; then
		info "Root filesystem already exists ($(<"${ROOT}/.gentoo-stage3")), not re-extracting"
		return
	fi
	info "Extracting ${file} to ${ROOT}"
	mkdir -p "${ROOT}"
	tar xpf "${STAGE3_DIR}/${file}" --xattrs-include='*.*' --numeric-owner -C "${ROOT}"
	echo "${file}" >"${ROOT}/.gentoo-stage3"
}

step_build() {
	[[ -f ${ROOT}/.gentoo-stage3 ]] || die "No root filesystem yet, run the 'fetch' step first"
	mount_chroot
	info "Building the ${EDITION} system inside the chroot (this takes hours)"
	run_chroot /mnt/gentoo-src/scripts/chroot/build-system.sh
	touch "${ROOT}/.gentoo-built"
}

step_iso() {
	[[ -f ${ROOT}/.gentoo-built ]] || die "System not built yet, run the 'build' step first"
	mount_chroot
	rm -rf "${ISOTREE}"
	# The ISO tree lives outside the rootfs, and the rootfs is bind mounted
	# non-recursively so the squashfs sees no /proc, /sys, caches, etc.
	bind_mount "${ISOTREE}" /mnt/isotree
	mkdir -p "${ROOT}/mnt/livesrc"
	mount --bind "${ROOT}" "${ROOT}/mnt/livesrc"
	mount -o remount,bind,ro "${ROOT}/mnt/livesrc"
	MOUNTS+=("${ROOT}/mnt/livesrc")

	info "Creating ${ISO_NAME}"
	run_chroot /mnt/gentoo-src/scripts/chroot/make-iso.sh
	umount_chroot
	rm -rf "${ISOTREE}"

	(cd "${OUT}" && sha256sum "${ISO_NAME}" >"${ISO_NAME}.sha256")
	info "Done: ${OUT}/${ISO_NAME}"
	info "Write it to a USB stick with: dd if=${OUT}/${ISO_NAME} of=/dev/sdX bs=4M status=progress oflag=sync"
}

# --------------------------------------------------------------------------
check_host
mkdir -p "${WORK}" "${OUT}" "${DISTFILES}" "${BINPKGS}"
info "${DISTRO_NAME} - edition ${EDITION}"
info "  CPU: ${CPU_DESC}"
info "  GPU: ${GPU_DESC}"

if ((CLEAN)); then
	# Never rm -rf through a leftover bind mount of /dev, /sys or the sources.
	if findmnt -rn -o TARGET | grep -qF "${EDITION_DIR}/"; then
		die "Something is still mounted below ${EDITION_DIR}; unmount it first (findmnt | grep ${EDITION})"
	fi
	info "Removing ${EDITION_DIR}"
	rm -rf "${EDITION_DIR}"
	exit 0
fi

IFS=, read -r -a steps <<<"${STEPS}"
for step in "${steps[@]}"; do
	case ${step} in
		fetch | build | iso) "step_${step}" ;;
		all) step_fetch; step_build; step_iso ;;
		*) die "Unknown step '${step}' (expected fetch, build or iso)" ;;
	esac
done
