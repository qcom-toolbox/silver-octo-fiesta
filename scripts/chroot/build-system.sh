#!/bin/bash
# Runs INSIDE the chroot (started by build.sh). Turns a stage3 into the
# finished KDE Plasma desktop. Safe to run again after a failure: finished
# work is skipped or comes from the binary package cache.
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

GPU_DIR="${SRC}/config/gpu/${GPU}"
STATE_DIR=/usr/share/gentoo-desktop
MARKERS=/var/lib/gentoo-desktop-build

: "${JOBS:=$(nproc)}" "${BINHOST:=0}" "${REBUILD:=1}" "${BUILD_DATE:=$(date +%Y%m%d)}"
: "${VM_HOST:=1}" "${MULTILIB:=1}" "${CHECK_ONLY:=0}"
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

# Print the packages of a list file, one per line. "a | b" means: the first of
# a, b that exists in the tree. Missing packages only warn.
resolve_list() {
	local line alt found
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
			echo "${found}"
		else
			warn "Not available in the tree, skipping: ${line}"
		fi
	done < <(list_atoms "$1")
}

# Install every package of a list file (see resolve_list).
install_list() {
	local -a pkgs
	mapfile -t pkgs < <(resolve_list "$1")
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
	render_template /etc/portage/make.conf DISTRO_NAME EDITION CPU_DESC GPU_DESC CPU_CFLAGS CPU_RUST \
		CPU_FLAGS_X86 JOBS EMERGE_JOBS VIDEO_CARDS

	# Kernel config fragment, merged by sys-kernel/gentoo-kernel.
	install -Dm644 "${SRC}/config/kernel/desktop.config" /etc/kernel/config.d/desktop.config

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

# 32-bit libraries (multilib) for Steam, Wine/Proton and other 32-bit programs:
# set abi_x86_32 on them and on every dependency that needs it too.
# Leaves the package list in MULTILIB_PKGS.
multilib_use() {
	local use=/etc/portage/package.use/30-multilib-32bit
	local -a pkgs
	mapfile -t pkgs < <(resolve_list "${SRC}/config/packages/multilib-32bit.list")
	MULTILIB_PKGS=("${pkgs[@]}")
	((${#pkgs[@]})) || return 0

	info "Enabling 32-bit (abi_x86_32) builds of ${#pkgs[@]} libraries for 32-bit programs"
	{
		echo "# 32-bit libraries for Steam, Wine/Proton and other 32-bit programs"
		echo "# (from config/packages/multilib-32bit.list, then the dependencies they need)"
		printf '%s abi_x86_32\n' "${pkgs[@]}"
		[[ ${GPU_ID} == nvidia ]] && echo "x11-drivers/nvidia-drivers abi_x86_32"
		echo "# --- dependencies (found by Portage) ---"
	} >"${use}"

	# The dependency chain is deep (e.g. libpulse -> libsndfile -> flac, ogg,
	# vorbis, opus, lame, mpg123 ...) and one autounmask run gives up on it.
	# So: take the abi_x86_32 changes Portage proposes, add them, and repeat
	# until the plan resolves. Only abi_x86_32 is ever added.
	local pass out new count=0
	for pass in $(seq 1 15); do
		if out=$(emerge --pretend --update --deep --newuse --backtrack=100 \
			--ignore-built-slot-operator-deps=y --autounmask=y --autounmask-use=y \
			--autounmask-backtrack=y --autounmask-write=n --autounmask-keep-keywords=y \
			--autounmask-keep-masks=y --autounmask-license=n @world "${pkgs[@]}" 2>&1); then
			info "32-bit dependencies complete: ${count} added in $((pass - 1)) passes"
			return 0
		fi
		new=$(grep -E '^[<>=~]*[a-z0-9-]+/[^ ]+ .*abi_x86_32' <<<"${out}" | sort -u |
			grep -vxF -f "${use}" || true)
		if [[ -z ${new} ]]; then
			warn "Portage could not work out all 32-bit dependencies (pass ${pass}); the next step shows why"
			return 0
		fi
		echo "${new}" >>"${use}"
		count=$((count + $(wc -l <<<"${new}")))
		info "Pass ${pass}: $(wc -l <<<"${new}") more libraries need a 32-bit build"
	done
	warn "32-bit dependencies still incomplete after ${pass} passes; the next step shows why"
}

setup_multilib() {
	multilib_use
	((${#MULTILIB_PKGS[@]})) || return 0
	emerge --update --deep --newuse --noreplace "${MULTILIB_PKGS[@]}" ||
		warn "Some 32-bit libraries could not be installed; 32-bit programs may be missing pieces"
}

# Go on its own first: in a --deep update Portage resolves Go's build
# dependency "|| ( go go-bootstrap )" to Go itself, a circular dependency
# (hit through plasma-meta -> plasma-vault -> gocryptfs). Installed alone it
# is built with go-bootstrap; afterwards the installed Go satisfies itself.
install_go() {
	if ! portageq has_version / dev-lang/go; then
		info "Installing Go (bootstrapped) before the desktop"
		emerge --oneshot --noreplace dev-lang/go
	fi
}

# build.sh --step check: resolve the complete package plan against the current
# Gentoo tree without compiling anything. Catches renamed or removed packages
# and USE flag conflicts in minutes instead of hours into a build.
check_plan() {
	local f log=/var/log/gentoo-desktop-check.log
	local -a pkgs=() list=() lists=("${SRC}/config/packages/extras.list" "${GPU_DIR}/packages.list")
	((VM_HOST)) && lists+=("${SRC}/config/packages/vm-host.list")
	for f in "${lists[@]}"; do
		mapfile -t list < <(resolve_list "${f}")
		pkgs+=("${list[@]}")
	done
	# shellcheck disable=SC2206 # a list of atoms
	[[ -n ${CPU_PACKAGES} ]] && pkgs+=(${CPU_PACKAGES})
	# Mirror the build's two phases: first the stage3 is recompiled (unless
	# --no-rebuild or --binhost), then everything else is a normal update.
	if ((REBUILD && !BINHOST)); then
		info "Phase 1: recompiling the stage3 (@world) for ${CPU_CFLAGS}"
		emerge --pretend --emptytree @world >"${log}" 2>&1 ||
			{ grep -v '^\[ebuild\|^\[binary\|^$' "${log}" | tail -n 40 >&2; die "Phase 1 does not resolve (see ${log})"; }
		info "OK: $(grep -c '^\[ebuild' "${log}") packages"
	fi

	# As in the build: Go is installed on its own (this compiles Go, a few
	# minutes; Portage cannot plan it otherwise), and the 32-bit libraries are
	# set up after the stage3 rebuild.
	install_go
	if ((MULTILIB)); then
		multilib_use
		pkgs+=("${MULTILIB_PKGS[@]}")
	fi

	# The build recompiles the stage3 first, which e.g. moves the installed
	# Perl modules to a new Perl. A dry run cannot do that, so ignore what
	# the installed packages were built against.
	info "Phase 2: @world, @desktop-core, @desktop-gpu and ${#pkgs[@]} more packages (no compiling)"
	if emerge --pretend --verbose --update --deep --newuse --with-bdeps=y --backtrack=100 \
		--ignore-built-slot-operator-deps=y \
		@world @desktop-core @desktop-gpu "${pkgs[@]}" >"${log}" 2>&1; then
		info "OK: the plan resolves. $(grep -c '^\[ebuild' "${log}") packages would be built,"
		info "$(grep -c '^\[binary' "${log}") would come from binary packages. Details: ${log}"
		if [[ -s /etc/portage/package.use/30-multilib-32bit ]]; then
			info "$(grep -c abi_x86_32 /etc/portage/package.use/30-multilib-32bit) packages are built in 32-bit as well."
		fi
	else
		warn "The plan does NOT resolve. Portage says:"
		grep -v '^\[ebuild\|^\[binary\|^$' "${log}" | tail -n 60 >&2
		die "Fix the configuration above (full output: ${log})"
	fi
}

build_world() {
	mkdir -p "${MARKERS}"
	emerge --oneshot --update sys-apps/portage

	# Recompile the generic stage3 for this CPU once. Re-runs resume quickly
	# because every finished package is in the binary package cache.
	if ((REBUILD && !BINHOST)) && [[ ! -f ${MARKERS}/world-rebuilt ]]; then
		info "Recompiling the whole stage3 with ${CPU_CFLAGS}"
		emerge --emptytree @world
		touch "${MARKERS}/world-rebuilt"
	fi

	info "Updating @world with the desktop USE flags"
	emerge --update --deep --newuse @world

	install_go
	info "Installing the core desktop (@desktop-core, @desktop-gpu)"
	emerge --update --deep --newuse --noreplace @desktop-core @desktop-gpu

	install_list "${SRC}/config/packages/extras.list"
	install_list "${GPU_DIR}/packages.list"
	if ((VM_HOST)); then
		install_list "${SRC}/config/packages/vm-host.list"
	fi
	if [[ -n ${CPU_PACKAGES} ]]; then
		info "Installing CPU specific packages: ${CPU_PACKAGES}"
		# shellcheck disable=SC2086 # a list of atoms
		emerge --noreplace ${CPU_PACKAGES}
	fi
	if ((MULTILIB)); then
		setup_multilib
	fi

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
	rm -rf /usr/lib/gentoo-installer
	mkdir -p /usr/lib/gentoo-installer
	cp -R --no-preserve=ownership "${SRC}/installer/gentoo_installer" /usr/lib/gentoo-installer/
	find /usr/lib/gentoo-installer -name __pycache__ -prune -exec rm -rf {} +
	install -Dm755 "${SRC}/installer/gentoo-installer" /usr/bin/gentoo-installer
	python3 -m compileall -q /usr/lib/gentoo-installer

	# neofetch was archived upstream; fall back to fastfetch if it left the tree.
	if ! command -v neofetch >/dev/null && command -v fastfetch >/dev/null; then
		warn "app-misc/neofetch not available, 'neofetch' will run fastfetch"
		printf '#!/bin/sh\nexec fastfetch "$@"\n' >/usr/local/bin/neofetch
		chmod 755 /usr/local/bin/neofetch
	fi

	# Snapshots are taken by /etc/cron.*/gentoo-snapshot; drop the cron jobs
	# some snapper versions install so nothing runs twice.
	rm -f /etc/cron.hourly/suse.de-snapper /etc/cron.daily/suse.de-snapper

	mkdir -p "${STATE_DIR}"
	cat >"${STATE_DIR}/edition.conf" <<-EOF
		DISTRO_NAME="${DISTRO_NAME}"
		DISTRO_SHORT="${DISTRO_SHORT}"
		DISTRO_ID="${DISTRO_ID}"
		EDITION="${EDITION}"
		CPU_ID="${CPU_ID}"
		CPU_DESC="${CPU_DESC}"
		CPU_VENDOR="${CPU_VENDOR}"
		CPU_CFLAGS="${CPU_CFLAGS}"
		CPU_REQUIRED_FLAGS="${CPU_REQUIRED_FLAGS}"
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
	# vm-guest starts QEMU/VMware/Hyper-V/VirtualBox/Xen guest tools when
	# running in that hypervisor; those services are not added themselves.
	for s in dbus NetworkManager display-manager bluetooth cupsd avahi-daemon chronyd sysklogd cronie vm-guest; do
		add_service "${s}" default
	done
	if ((VM_HOST)) && [[ -e /etc/init.d/libvirtd ]]; then
		add_service libvirtd default
		# Start libvirt's NAT network "default" with the daemon, so new VMs
		# in virt-manager have internet access right away.
		local net=/etc/libvirt/qemu/networks
		if [[ -f ${net}/default.xml ]]; then
			mkdir -p "${net}/autostart"
			ln -sf ../default.xml "${net}/autostart/default.xml"
		fi
	fi

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
	for g in users wheel audio video input render plugdev usb lp pipewire kvm libvirt vboxusers; do
		getent group "${g}" >/dev/null && groups+=("${g}")
	done
	if ! id "${LIVE_USER}" >/dev/null 2>&1; then
		useradd -m -c "Live User" -s /bin/bash -G "$(IFS=,; echo "${groups[*]}")" "${LIVE_USER}"
	fi
	passwd -d "${LIVE_USER}" >/dev/null
	passwd -l root >/dev/null

	local home="/home/${LIVE_USER}"
	install -Dm755 /usr/share/applications/gentoo-installer.desktop "${home}/Desktop/gentoo-installer.desktop"
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
if ((CHECK_ONLY)); then
	check_plan
	exit 0
fi
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
