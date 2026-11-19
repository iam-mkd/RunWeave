"""Create or drop a throwaway PostgreSQL database for the browser smoke test.

    python scripts/smoke-database.py create postgresql+asyncpg://user:pw@host/postgres
        -> prints the URL of a new, empty database
    python scripts/smoke-database.py drop <that URL>
"""

import argparse
import asyncio
import re
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

SMOKE_NAME = re.compile(r"^runweave_smoke_[0-9a-f]{12}$")


async def create(admin_url: str) -> str:
    url = make_url(admin_url)
    name = f"runweave_smoke_{uuid4().hex[:12]}"
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await engine.dispose()
    return url.set(database=name).render_as_string(hide_password=False)


async def drop(database_url: str) -> None:
    url = make_url(database_url)
    if not SMOKE_NAME.match(url.database or ""):
        raise SystemExit("Refusing to drop a database the smoke test did not create")
    engine = create_async_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as conn:
            await conn.execute(text(f'DROP DATABASE IF EXISTS "{url.database}" WITH (FORCE)'))
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("action", choices=("create", "drop"))
    parser.add_argument("url")
    args = parser.parse_args()
    if args.action == "create":
        print(asyncio.run(create(args.url)))
    else:
        asyncio.run(drop(args.url))


if __name__ == "__main__":
    main()
