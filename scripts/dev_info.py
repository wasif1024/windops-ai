#!/usr/bin/env python3
"""What scripts/dev.sh needs from the cluster: the CA certificate, and the demo users.

python scripts/dev_info.py ca       # write the cluster's CA certificate, and print its path
python scripts/dev_info.py logins   # print the demo users' emails; make demo-logins has passwords
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
    """Return (who, email, password key) for each demo user, which exist only on a local cluster.

    The key names the user's entry in `make demo-logins`. Passwords are never printed from here.
    """
    found = []
    for alias in TENANTS:
        found.append((f"{alias} operator", demo_email(alias), alias))
        found.append((f"{alias} administrator", demo_admin_email(alias), demo_admin_key(alias)))
    found.append(("Platform Administrator", PLATFORM_ADMIN_EMAIL, PLATFORM_ADMIN_KEY))
    return found


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command == "ca":
        print(authority_file())
        return 0
    if command == "logins":
        rows = logins()
        if not read_demo_passwords():
            print("  None yet: run make identity-seed")
            return 0
        who_width = max(len(who) for who, _, _ in rows)
        email_width = max(len(email) for _, email, _ in rows)
        for who, email, key in rows:
            print(f"  {who:<{who_width}}  {email:<{email_width}}  ({key})")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
