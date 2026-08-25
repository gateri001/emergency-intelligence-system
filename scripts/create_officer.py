"""
Creates an officer login. No default/hardcoded credentials ship in source -
you set your own on first run.
Run: python scripts/create_officer.py <username>
"""
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.auth import hash_password
from src.database import get_connection, init_db


def main():
    if len(sys.argv) != 2:
        print("Usage: python scripts/create_officer.py <username>")
        sys.exit(1)
    username = sys.argv[1]
    password = getpass.getpass(f"Password for '{username}': ")

    init_db()
    conn = get_connection()
    conn.execute(
        "INSERT INTO officers (username, hashed_password) VALUES (?, ?)",
        (username, hash_password(password)),
    )
    conn.commit()
    conn.close()
    print(f"Officer '{username}' created.")


if __name__ == "__main__":
    main()
