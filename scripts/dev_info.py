#!/usr/bin/env python3
"""What scripts/dev.sh needs from the cluster: the CA certificate, and the demo sign-ins.

python scripts/dev_info.py ca       # write the cluster's CA certificate, and print its path
python scripts/dev_info.py logins   # print the demo users' emails and passwords
"""

import sys

from identity_seed import (
    PLATFORM_ADMIN_EMAIL,
    PLATFORM_ADMIN_KEY,
    TENANTS,
    demo_admin_email,
    demo_admin_key,
    demo_email,
    read_demo_passwords,
)
from tls_client import authority_file


def logins() -> list[tuple[str, str, str]]:
    """Return (who, email, password) for each demo user. These exist only on a local cluster."""
    passwords = read_demo_passwords()
    found = []
    for alias in TENANTS:
        found.append((f"{alias} operator", demo_email(alias), passwords.get(alias, "")))
        found.append(
            (
                f"{alias} administrator",
                demo_admin_email(alias),
                passwords.get(demo_admin_key(alias), ""),
            )
        )
    found.append(
        ("Platform Administrator", PLATFORM_ADMIN_EMAIL, passwords.get(PLATFORM_ADMIN_KEY, ""))
    )
    return found


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "ca":
        print(authority_file())
        return 0
    if command == "logins":
        rows = logins()
        who_width = max(len(who) for who, _, _ in rows)
        email_width = max(len(email) for _, email, _ in rows)
        for who, email, password in rows:
            shown = password or "(run make identity-seed)"
            print(f"  {who:<{who_width}}  {email:<{email_width}}  {shown}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
