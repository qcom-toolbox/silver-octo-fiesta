"""Entry point: python3 -m gentoo_installer [--dry-run]"""

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(prog="gentoo-installer", description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="walk through the installer and log every command without changing anything")
    parser.add_argument("--edition", default=None, help="path to an edition.conf (testing)")
    args, qt_args = parser.parse_known_args()

    if not args.dry_run and os.geteuid() != 0:
        print("The installer must run as root (or use --dry-run).", file=sys.stderr)
        return 1

    from .ui import run_app

    return run_app(dry_run=args.dry_run, edition_path=args.edition, qt_args=[sys.argv[0], *qt_args])


if __name__ == "__main__":
    sys.exit(main())
