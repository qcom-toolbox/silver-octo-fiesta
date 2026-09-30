#!/bin/bash
# Runs INSIDE the chroot (started by build.sh): builds the live initramfs,
# the compressed root filesystem and the ISO (UEFI, legacy BIOS or both).
#   /mnt/livesrc  read-only, non-recursive bind mount of the finished rootfs
#   /mnt/isotree  empty directory that becomes the ISO contents
#   /mnt/gentoo-out  output directory
set -euo pipefail

SRC=/mnt/gentoo-src
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
	'var/lib/gentoo-desktop-build' '.gentoo-*' 'root/.bash_history'

# Used through render_template.
# shellcheck disable=SC2034
CPU_DESC_SHORT=${CPU_DESC%% (*}
# shellcheck disable=SC2034
GPU_DESC_SHORT=${GPU_DESC%% (*}
cp "${SRC}/iso/grub.cfg.in" "${TREE}/boot/grub/grub.cfg"
render_template "${TREE}/boot/grub/grub.cfg" DISTRO_NAME EDITION ISO_LABEL CPU_DESC_SHORT GPU_DESC_SHORT
cp "/usr/share/gentoo-desktop/edition.conf" "${TREE}/edition.conf"

# build.sh --boot: grub-mkrescue puts every installed GRUB platform on the ISO;
# -d limits it to one.
case ${BOOT:-both} in
	uefi) mkrescue_platform=(-d /usr/lib/grub/x86_64-efi) boot_desc="UEFI" ;;
	bios) mkrescue_platform=(-d /usr/lib/grub/i386-pc) boot_desc="legacy BIOS" ;;
	*) mkrescue_platform=() boot_desc="hybrid UEFI + legacy BIOS" ;;
esac
for d in "${mkrescue_platform[@]:1}"; do
	[[ -d ${d} ]] || die "GRUB for ${boot_desc} is not installed (${d})"
done
info "Creating the ${boot_desc} ISO"
# The squashfs is larger than 4 GiB, the limit for one file in ISO 9660 at the
# default level; level 3 stores it in several extents (Linux reads that fine).
# grub-mkrescue runs "xorriso -as mkisofs <its args and our tree> -- <ours>",
# so the level must go into the mkisofs part, through a small wrapper.
xorriso_wrapper=$(mktemp)
cat >"${xorriso_wrapper}" <<'WRAPPER'
#!/bin/sh
if [ "$1" = "-as" ] && [ "$2" = "mkisofs" ]; then
	shift 2
	exec xorriso -as mkisofs -iso-level 3 "$@"
fi
exec xorriso "$@"
WRAPPER
chmod 755 "${xorriso_wrapper}"
grub-mkrescue "${mkrescue_platform[@]}" --xorriso="${xorriso_wrapper}" -o "/mnt/gentoo-out/${ISO_NAME}" "${TREE}" -- -volid "${ISO_LABEL}"
rm -f "${xorriso_wrapper}"
info "ISO size: $(du -h "/mnt/gentoo-out/${ISO_NAME}" | cut -f1)"
