#!/bin/bash
# Runs INSIDE the chroot (started by build.sh): builds the live initramfs,
# the compressed root filesystem and the hybrid BIOS/UEFI ISO.
#   /mnt/livesrc  read-only, non-recursive bind mount of the finished rootfs
#   /mnt/isotree  empty directory that becomes the ISO contents
#   /mnt/am4-out  output directory
set -euo pipefail

SRC=/mnt/am4-src
# shellcheck source=../lib.sh
source "${SRC}/scripts/lib.sh"
# shellcheck source=../../config/distro.conf
source "${SRC}/config/distro.conf"
# shellcheck source=../../config/cpu/zen3.conf
source "${SRC}/config/cpu/${CPU:?}.conf"
# shellcheck source=../../config/gpu/nvidia/gpu.conf
source "${SRC}/config/gpu/${GPU:?}/gpu.conf"

TREE=/mnt/isotree
: "${ISO_NAME:?}" "${ISO_LABEL:?}"

KVER=$(find /lib/modules -mindepth 1 -maxdepth 1 -printf '%f\n' | sort -V | tail -n1)
[[ -n ${KVER} ]] || die "No kernel installed"
KIMAGE=
for k in "/usr/lib/modules/${KVER}/vmlinuz" "/boot/vmlinuz-${KVER}" "/boot/kernel-${KVER}"; do
	if [[ -f ${k} ]]; then
		KIMAGE=${k}
		break
	fi
done
[[ -n ${KIMAGE} ]] || die "Kernel image for ${KVER} not found"

mkdir -p "${TREE}/boot/grub" "${TREE}/LiveOS"
info "Kernel ${KVER} (${KIMAGE})"
cp "${KIMAGE}" "${TREE}/boot/vmlinuz"

info "Building the live initramfs (dracut dmsquash-live)"
dracut --force --no-hostonly --kver "${KVER}" \
	--add "dmsquash-live" --omit "plymouth" --compress zstd \
	"${TREE}/boot/initramfs.img"

info "Compressing the root filesystem (squashfs, zstd)"
mksquashfs /mnt/livesrc "${TREE}/LiveOS/squashfs.img" \
	-noappend -comp zstd -Xcompression-level 19 -b 1M -processors "${JOBS:-$(nproc)}" \
	-wildcards -e \
	'proc/*' 'sys/*' 'dev/*' 'run/*' 'tmp/*' 'mnt/*' \
	'var/tmp/*' 'var/cache/distfiles/*' 'var/cache/binpkgs/*' \
	'var/lib/am4-build' '.am4-*' 'root/.bash_history'

# Used through render_template.
# shellcheck disable=SC2034
CPU_DESC_SHORT=${CPU_DESC%% (*}
# shellcheck disable=SC2034
GPU_DESC_SHORT=${GPU_DESC%% (*}
cp "${SRC}/iso/grub.cfg.in" "${TREE}/boot/grub/grub.cfg"
render_template "${TREE}/boot/grub/grub.cfg" DISTRO_NAME EDITION ISO_LABEL CPU_DESC_SHORT GPU_DESC_SHORT
cp "/usr/share/am4/edition.conf" "${TREE}/edition.conf"

info "Creating the hybrid BIOS/UEFI ISO"
grub-mkrescue -o "/mnt/am4-out/${ISO_NAME}" "${TREE}" -- -volid "${ISO_LABEL}"
info "ISO size: $(du -h "/mnt/am4-out/${ISO_NAME}" | cut -f1)"
