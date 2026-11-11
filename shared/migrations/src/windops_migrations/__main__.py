"""Run migrations: python -m windops_migrations upgrade windops_audit.

On Azure, `provision-entra` first gives each service its database and Entra login (provision.py).
"""

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
    entra = commands.add_parser("provision-entra", help="Each service's database and Entra login")
    entra.add_argument("--host", required=True, help="the Azure Database for PostgreSQL server")
    entra.add_argument("--admin", required=True, help="the server's Entra administrator")
    entra.add_argument("--role", action="append", required=True, help="principal=object-id=db")
    parsed = parser.parse_args(arguments)
    if parsed.command == "upgrade":
        upgrade(parsed.package)
    elif parsed.command == "provision-entra":
        from windops_migrations.provision import Role, provision

        provision(parsed.host, parsed.admin, [Role.parse(text) for text in parsed.role])
    else:
        print(create_revision(parsed.package, parsed.message))
    return 0


if __name__ == "__main__":
    sys.exit(main())
