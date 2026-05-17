from __future__ import annotations

import argparse
import getpass
import sys

from sqlalchemy import select

from app.bootstrap import init_database
from app.database import SessionLocal
from app.models import User
from app.security import hash_password


def create_admin(args: argparse.Namespace) -> int:
    init_database()
    password = args.password or getpass.getpass("Password: ")
    if len(password) < 12:
        print("Password must be at least 12 characters", file=sys.stderr)
        return 2
    with SessionLocal() as db:
        existing = db.scalar(select(User).where(User.username == args.username))
        if existing:
            existing.password_hash = hash_password(password)
            existing.is_active = True
            action = "Updated"
        else:
            db.add(User(username=args.username, password_hash=hash_password(password)))
            action = "Created"
        db.commit()
    print(f"{action} admin user {args.username}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="pxe-app administration")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="create or reset an admin user")
    admin.add_argument("username")
    admin.add_argument("--password")
    admin.set_defaults(func=create_admin)
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

