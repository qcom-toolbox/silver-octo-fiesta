#!/bin/sh
# Join the pieces back into the ISO and check it (Linux / macOS).
set -e
cd "$(dirname "$0")"
cat gentoo-desktop-generic-mesa-20260927.iso.[0-9][0-9][0-9] > gentoo-desktop-generic-mesa-20260927.iso
if command -v sha256sum >/dev/null; then sha256sum -c SHA256SUMS; else shasum -a 256 -c SHA256SUMS; fi
