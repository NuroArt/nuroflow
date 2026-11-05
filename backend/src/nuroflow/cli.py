"""``nuroflow`` management CLI.

    nuroflow create-admin --email you@example.com      # prompts for the password

Bootstrapping the first admin is a CLI task on purpose. "First registered user becomes
admin" is a race condition and a classic takeover bug on freshly deployed instances.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys

from nuroflow.core.config import get_settings
from nuroflow.core.exceptions import NuroFlowError
from nuroflow.domain.enums import UserRole


async def _create_admin(email: str, password: str, full_name: str | None) -> str:
    from nuroflow.api.container import build_container
    from nuroflow.repositories.sqlalchemy import SqlAlchemyUnitOfWork
    from nuroflow.services.auth import AuthService, normalize_email

    container = build_container(get_settings())
    try:
        async with container.session_factory() as session:
            uow = SqlAlchemyUnitOfWork(session)
            existing = await uow.users.get_by_email(normalize_email(email))
            if existing is not None:
                existing.role = UserRole.ADMIN
                existing.is_active = True
                await uow.commit()
                return f"Promoted existing user {existing.email} to admin."
            auth = AuthService(
                uow,
                hasher=container.password_hasher,
                jwt=container.jwt,
                clock=container.clock,
                policy=container.auth_policy,
            )
            user = await auth.register(
                email=email,
                password=password,
                full_name=full_name,
                role=UserRole.ADMIN,
                bypass_registration_policy=True,
            )
            return f"Created admin {user.email} ({user.id})."
    finally:
        await container.aclose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nuroflow", description="NuroFlow management commands")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="create an admin user (or promote an existing one)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--full-name")
    args = parser.parse_args(argv)

    if args.command == "create-admin":
        # Non-interactive use (CI, containers): NUROFLOW_ADMIN_PASSWORD. Never a CLI flag,
        # because flags show up in shell history and `ps` output.
        password = os.environ.get("NUROFLOW_ADMIN_PASSWORD") or getpass.getpass("Password: ")
        try:
            print(asyncio.run(_create_admin(args.email, password, args.full_name)))
        except NuroFlowError as exc:
            print(f"error: {exc.detail}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
