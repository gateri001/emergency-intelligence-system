"""
Creates a responder login. No default/hardcoded credentials ship in source -
you set your own on first run.
Run: python scripts/create_officer.py <username> [--role officer|admin|owner]

Roles:
  officer  reviews reports, verifies missing-child cases (with an OB number),
           sends ordinary SMS alerts, proposes missing-child alerts
  admin    a trusted person: also approves missing-child alerts and can open
           a crime reporter's number (written reason, logged)
  owner    the founder: everything, may send a missing-child alert alone,
           reads the audit log
"""
import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import ROLE_RANK, hash_password
from src.database import get_connection, init_db

MIN_PASSWORD_LENGTH = 12


def main():
    parser = argparse.ArgumentParser(description="Create a responder login.")
    parser.add_argument("username")
    parser.add_argument("--role", choices=sorted(ROLE_RANK), default="officer")
    args = parser.parse_args()

    password = getpass.getpass(f"Password for '{args.username}' (at least {MIN_PASSWORD_LENGTH} characters): ")
    if len(password) < MIN_PASSWORD_LENGTH:
        print(f"Password too short; use at least {MIN_PASSWORD_LENGTH} characters.")
        sys.exit(1)
    if getpass.getpass("Again: ") != password:
        print("Passwords did not match.")
        sys.exit(1)

    init_db()
    conn = get_connection()
    conn.execute(
        "INSERT INTO officers (username, hashed_password, role) VALUES (?, ?, ?)",
        (args.username, hash_password(password), args.role),
    )
    conn.commit()
    conn.close()
    print(f"'{args.username}' created with role '{args.role}'.")


if __name__ == "__main__":
    main()
