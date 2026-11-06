"""``nuroflow`` management CLI.

    nuroflow create-admin --email you@example.com      # prompts for the password
    nuroflow healthcheck api|worker|scheduler          # container health probes
    nuroflow rotate-encryption                         # after prepending a new key

Bootstrapping the first admin is a CLI task on purpose. "First registered user becomes
admin" is a race condition and a classic takeover bug on freshly deployed instances.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import re
import sys
import time
import urllib.request
from collections.abc import Callable

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


def healthcheck(
    role: str,
    *,
    max_tick_age: float = 60.0,
    fetch: Callable[[str], str] | None = None,
    now: Callable[[], float] = time.time,
) -> tuple[bool, str]:
    """Docker HEALTHCHECK for each role, with no curl in the image (smaller attack surface).

    * api: ``/healthz``, liveness only. Readiness (DB, Redis) is the load balancer's job;
      restarting a container doesn't fix a database outage.
    * worker: the metrics server in the Celery parent answers.
    * scheduler: the last tick is recent. A *stuck* loop (deadlock, hung query) still
      answers HTTP, so "process is up" isn't enough.
    """
    settings = get_settings()
    fetch = fetch or _http_get
    urls = {
        "api": "http://127.0.0.1:8000/healthz",
        "worker": f"http://127.0.0.1:{settings.worker_metrics_port}/metrics",
        "scheduler": f"http://127.0.0.1:{settings.scheduler_metrics_port}/metrics",
    }
    try:
        body = fetch(urls[role])
    except Exception as exc:
        return False, f"{role}: unreachable ({type(exc).__name__})"
    if role != "scheduler":
        return True, f"{role}: ok"
    match = re.search(r"^nuroflow_scheduler_last_tick_timestamp_seconds (\S+)$", body, re.M)
    if not match:
        return False, "scheduler: no tick recorded yet"
    age = now() - float(match.group(1))
    return age <= max_tick_age, f"scheduler: last tick {age:.1f}s ago"


def _http_get(url: str) -> str:
    with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310 (fixed localhost URLs)
        body: bytes = response.read()
    return body.decode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="nuroflow", description="NuroFlow management commands")
    sub = parser.add_subparsers(dest="command", required=True)
    admin = sub.add_parser("create-admin", help="create an admin user (or promote an existing one)")
    admin.add_argument("--email", required=True)
    admin.add_argument("--full-name")
    probe = sub.add_parser(
        "healthcheck", help="container health probe for one role (exit 0 = healthy)"
    )
    probe.add_argument("role", choices=["api", "worker", "scheduler"])
    probe.add_argument("--max-tick-age", type=float, default=60.0)
    sub.add_parser(
        "rotate-encryption",
        help="re-encrypt every stored secret with the first NUROFLOW_ENCRYPTION_KEYS key",
    )
    args = parser.parse_args(argv)

    if args.command == "rotate-encryption":
        from nuroflow.core.crypto import SecretBox
        from nuroflow.db.session import create_sync_db_engine, create_sync_session_factory
        from nuroflow.maintenance import rotate_encryption

        settings = get_settings()
        engine = create_sync_db_engine(settings)
        try:
            counts = rotate_encryption(
                create_sync_session_factory(engine), SecretBox.from_settings(settings)
            )
        finally:
            engine.dispose()
        print(", ".join(f"{table}: {n} re-encrypted" for table, n in counts.items()))
        return 0

    if args.command == "healthcheck":
        healthy, detail = healthcheck(args.role, max_tick_age=args.max_tick_age)
        print(detail)
        return 0 if healthy else 1

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
