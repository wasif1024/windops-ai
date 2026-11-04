"""Run migrations: python -m windops_migrations upgrade windops_audit."""

import argparse
import sys

from windops_migrations.runner import create_revision, upgrade


def main(arguments: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="windops_migrations", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("upgrade", help="Apply every new revision").add_argument("package")
    new = commands.add_parser("new", help="Add an empty revision")
    new.add_argument("package")
    new.add_argument("--message", required=True)
    parsed = parser.parse_args(arguments)
    if parsed.command == "upgrade":
        upgrade(parsed.package)
    else:
        print(create_revision(parsed.package, parsed.message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
