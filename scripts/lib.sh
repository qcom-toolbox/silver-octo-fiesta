# Shared helpers for the host build script and the chroot scripts.
# shellcheck shell=bash

if [[ -t 1 ]]; then
	_c_info=$'\e[1;32m' _c_warn=$'\e[1;33m' _c_err=$'\e[1;31m' _c_off=$'\e[0m'
else
	_c_info='' _c_warn='' _c_err='' _c_off=''
fi

info() { printf '%s>>>%s %s\n' "${_c_info}" "${_c_off}" "$*"; }
warn() { printf '%s!!!%s %s\n' "${_c_warn}" "${_c_off}" "$*" >&2; }
die() {
	printf '%s***%s %s\n' "${_c_err}" "${_c_off}" "$*" >&2
	exit 1
}

# Replace @KEY@ placeholders in a file with the value of the shell variable KEY.
# Usage: render_template <file> KEY...
render_template() {
	local file=$1 key value
	shift
	for key in "$@"; do
		value=${!key}
		value=${value//\\/\\\\}
		value=${value//|/\\|}
		value=${value//&/\\&}
		sed -i "s|@${key}@|${value}|g" "${file}"
	done
}

# Print the atoms of a package list file (comments and blank lines removed).
list_atoms() {
	sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' "$1" | grep -v '^$' || true
}
