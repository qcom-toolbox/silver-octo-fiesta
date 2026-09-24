#!/bin/bash
# Runs INSIDE the chroot (started by build.sh). Turns a stage3 into the
# finished KDE Plasma desktop. Safe to run again after a failure: finished
# work is skipped or comes from the binary package cache.
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

GPU_DIR="${SRC}/config/gpu/${GPU}"
STATE_DIR=/usr/share/am4
MARKERS=/var/lib/am4-build

: "${JOBS:=$(nproc)}" "${BINHOST:=0}" "${REBUILD:=1}" "${BUILD_DATE:=$(date +%Y%m%d)}"
EMERGE_JOBS=2
((JOBS >= 12)) && EMERGE_JOBS=3
((JOBS >= 24)) && EMERGE_JOBS=4
export EDITION EMERGE_JOBS

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

# Make /etc/portage/<name> a directory (stage3s sometimes ship a file).
portage_dir() {
	local d="/etc/portage/$1"
	if [[ -f ${d} ]]; then
		mv "${d}" "${d}.stage3"
		mkdir -p "${d}"
		mv "${d}.stage3" "${d}/zz-stage3"
	fi
	mkdir -p "${d}"
}

# Copy a directory tree onto / as root-owned files.
install_tree() {
	local src=$1
	[[ -d ${src} ]] || return 0
	cp -dR --no-preserve=ownership --preserve=mode,timestamps "${src}/." /
	if [[ -d ${src}/etc/sudoers.d ]]; then
		chmod 0440 /etc/sudoers.d/*
	fi
	# Our files win over package defaults: drop pending config updates for them.
	local f
	while IFS= read -r -d '' f; do
		f=${f#"${src}"}
		rm -f "$(dirname "${f}")"/._cfg????_"$(basename "${f}")"
		if grep -qs '@DISTRO_' "${f}"; then
			render_template "${f}" DISTRO_NAME DISTRO_SHORT DISTRO_ID
		fi
	done < <(find "${src}" -type f -print0)
}

add_service() { # <service> <runlevel>
	if [[ -e /etc/init.d/$1 ]]; then
		rc-update add "$1" "$2" >/dev/null
	else
		warn "Service $1 is not installed, not enabling it"
	fi
}

# Install every line of a package list. "a | b" means: the first of a, b that
# exists in the tree. Missing packages only warn.
install_list() {
	local line alt found pkgs=()
	while IFS= read -r line; do
		found=
		IFS='|' read -r -a alts <<<"${line}"
		for alt in "${alts[@]}"; do
			alt=$(echo "${alt}" | xargs)
			if [[ -n $(portageq best_visible / "${alt}" 2>/dev/null) ]]; then
				found=${alt}
				break
			fi
		done
		if [[ -n ${found} ]]; then
			pkgs+=("${found}")
		else
			warn "Not available in the tree, skipping: ${line}"
		fi
	done < <(list_atoms "$1")
	((${#pkgs[@]})) || return 0

	info "Installing ${#pkgs[@]} packages from ${1#"${SRC}"/}"
	if ! emerge --noreplace "${pkgs[@]}"; then
		warn "Bulk install failed, retrying packages one by one"
		local p failed=()
		for p in "${pkgs[@]}"; do
			emerge --noreplace "${p}" || failed+=("${p}")
		done
		((${#failed[@]} == 0)) || warn "These packages could not be installed: ${failed[*]}"
	fi
}

# --------------------------------------------------------------------------
# steps
# --------------------------------------------------------------------------
setup_portage() {
	info "Configuring Portage for ${CPU_DESC} / ${GPU_DESC}"
	local d
	for d in package.use package.license package.accept_keywords sets; do
		portage_dir "${d}"
		cp -R --no-preserve=ownership "${SRC}/config/portage/${d}/." "/etc/portage/${d}/"
		if [[ -d ${GPU_DIR}/portage/${d} ]]; then
			cp -R --no-preserve=ownership "${GPU_DIR}/portage/${d}/." "/etc/portage/${d}/"
		fi
	done

	cp "${SRC}/config/portage/make.conf.in" /etc/portage/make.conf
	render_template /etc/portage/make.conf DISTRO_NAME EDITION CPU_DESC GPU_DESC CPU_MARCH \
		CPU_FLAGS_X86 JOBS EMERGE_JOBS VIDEO_CARDS

	# Kernel config fragment, merged by sys-kernel/gentoo-kernel.
	install -Dm644 "${SRC}/config/kernel/am4.config" /etc/kernel/config.d/am4.config

	if ((BINHOST)); then
		info "Enabling the official Gentoo x86-64-v3 binary package host"
		mkdir -p /etc/portage/binrepos.conf
		cat >/etc/portage/binrepos.conf/gentoobinhost.conf <<-EOF
			[gentoobinhost]
			priority = 1
			sync-uri = https://distfiles.gentoo.org/releases/amd64/binpackages/23.0/x86-64-v3/
		EOF
		# shellcheck disable=SC2016 # ${FEATURES} is for make.conf, not this shell
		sed -i '/^# END build-only/i FEATURES="${FEATURES} getbinpkg binpkg-request-signature"' \
			/etc/portage/make.conf
	fi
}

sync_tree() {
	local stamp=/var/db/repos/gentoo/metadata/timestamp.chk
	if [[ ! -f ${stamp} ]] || [[ -n $(find "${stamp}" -mmin +720) ]]; then
		info "Syncing the Gentoo repository"
		emerge-webrsync
	fi
	((BINHOST)) && getuto
	info "Selecting profile ${PORTAGE_PROFILE}"
	eselect profile set "${PORTAGE_PROFILE}"
}

setup_locale() {
	info "Generating locales"
	cat >/etc/locale.gen <<-EOF
		en_US.UTF-8 UTF-8
		C.UTF8 UTF-8
	EOF
	locale-gen
	eselect locale set "${DEFAULT_LOCALE%%.*}.utf8" || eselect locale set C.utf8
	env-update
}

build_world() {
	mkdir -p "${MARKERS}"
	emerge --oneshot --update sys-apps/portage

	# Recompile the generic stage3 for this CPU once. Re-runs resume quickly
	# because every finished package is in the binary package cache.
	if ((REBUILD && !BINHOST)) && [[ ! -f ${MARKERS}/world-rebuilt ]]; then
		info "Recompiling the whole stage3 with -march=${CPU_MARCH}"
		emerge --emptytree @world
		touch "${MARKERS}/world-rebuilt"
	fi

	info "Updating @world with the desktop USE flags"
	emerge --update --deep --newuse @world

	info "Installing the core desktop (@am4-core, @am4-gpu)"
	emerge --update --deep --newuse --noreplace @am4-core @am4-gpu

	install_list "${SRC}/config/packages/extras.list"
	install_list "${GPU_DIR}/packages.list"

	emerge --update --deep --newuse @world
	emerge --depclean

	python3 -c 'import PySide6' 2>/dev/null || python3 -c 'import PyQt6' 2>/dev/null ||
		die "Neither PySide6 nor PyQt6 is installed; the installer cannot run"
}

install_files() {
	info "Installing ${DISTRO_NAME} configuration files"
	install_tree "${SRC}/rootfs"
	install_tree "${GPU_DIR}/rootfs"

	# Graphical installer
	rm -rf /usr/lib/am4-installer
	mkdir -p /usr/lib/am4-installer
	cp -R --no-preserve=ownership "${SRC}/installer/am4_installer" /usr/lib/am4-installer/
	find /usr/lib/am4-installer -name __pycache__ -prune -exec rm -rf {} +
	install -Dm755 "${SRC}/installer/am4-installer" /usr/bin/am4-installer
	python3 -m compileall -q /usr/lib/am4-installer

	# neofetch was archived upstream; fall back to fastfetch if it left the tree.
	if ! command -v neofetch >/dev/null && command -v fastfetch >/dev/null; then
		warn "app-misc/neofetch not available, 'neofetch' will run fastfetch"
		printf '#!/bin/sh\nexec fastfetch "$@"\n' >/usr/local/bin/neofetch
		chmod 755 /usr/local/bin/neofetch
	fi

	mkdir -p "${STATE_DIR}"
	cat >"${STATE_DIR}/edition.conf" <<-EOF
		DISTRO_NAME="${DISTRO_NAME}"
		DISTRO_SHORT="${DISTRO_SHORT}"
		DISTRO_ID="${DISTRO_ID}"
		EDITION="${EDITION}"
		CPU_ID="${CPU_ID}"
		CPU_DESC="${CPU_DESC}"
		CPU_MARCH="${CPU_MARCH}"
		CPU_MIN_FAMILY=${CPU_MIN_FAMILY}
		GPU_ID="${GPU_ID}"
		GPU_DESC="${GPU_DESC}"
		BUILD_DATE="${BUILD_DATE}"
		LIVE_USER="${LIVE_USER}"
	EOF
}

setup_system() {
	info "Enabling OpenRC services"
	add_service elogind boot
	add_service zram-init boot
	local s
	for s in dbus NetworkManager display-manager bluetooth cupsd avahi-daemon chronyd sysklogd cronie; do
		add_service "${s}" default
	done

	echo "${DISTRO_ID}" >/etc/hostname
	echo "hostname=\"${DISTRO_ID}\"" >/etc/conf.d/hostname
	echo 'UTC' >/etc/timezone
	ln -sf ../usr/share/zoneinfo/UTC /etc/localtime

	if command -v flatpak >/dev/null; then
		flatpak remote-add --system --if-not-exists flathub \
			https://dl.flathub.org/repo/flathub.flatpakrepo || warn "Could not add the Flathub remote"
	fi
}

setup_live() {
	info "Setting up the live session (user '${LIVE_USER}', autologin to Plasma Wayland)"
	install_tree "${SRC}/rootfs-live"
	(cd "${SRC}/rootfs-live" && find . \( -type f -o -type l \) | sed 's|^\.||' | sort) \
		>"${STATE_DIR}/live-files.list"
	echo "${STATE_DIR}/live-files.list" >>"${STATE_DIR}/live-files.list"

	local g groups=()
	for g in users wheel audio video input render plugdev usb lp pipewire; do
		getent group "${g}" >/dev/null && groups+=("${g}")
	done
	if ! id "${LIVE_USER}" >/dev/null 2>&1; then
		useradd -m -c "Live User" -s /bin/bash -G "$(IFS=,; echo "${groups[*]}")" "${LIVE_USER}"
	fi
	passwd -d "${LIVE_USER}" >/dev/null
	passwd -l root >/dev/null

	local home="/home/${LIVE_USER}"
	install -Dm755 /usr/share/applications/am4-installer.desktop "${home}/Desktop/am4-installer.desktop"
	chown -R "${LIVE_USER}:${LIVE_USER}" "${home}"
}

finalize() {
	info "Cleaning up"
	sed -i '/^# BEGIN build-only/,/^# END build-only/d' /etc/portage/make.conf
	eselect news read all >/dev/null 2>&1 || true
	if command -v eix-update >/dev/null; then
		eix-update -q || warn "eix-update failed"
	fi
	if command -v updatedb >/dev/null; then
		updatedb || warn "updatedb failed"
	fi
	# Generated again on first boot, so every install gets its own ids.
	rm -f /etc/machine-id /var/lib/dbus/machine-id
	rm -rf /var/tmp/portage/* /tmp/* /root/.cache
	find /etc -name '._cfg????_*' -print | sed 's/^/    pending config update: /' || true
}

setup_portage
sync_tree
setup_locale
# Common config files go in before the packages (e.g. the dracut settings for
# the kernel's initramfs) and are re-applied afterwards. The GPU files come
# later: the NVIDIA dracut config needs nvidia-drivers to be built already.
install_tree "${SRC}/rootfs"
build_world
install_files
info "Regenerating the kernel's initramfs with the final configuration"
emerge --config sys-kernel/gentoo-kernel
setup_system
setup_live
finalize
info "System build for ${EDITION} finished"
