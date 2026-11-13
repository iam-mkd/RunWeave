import asyncio

from alembic import context  # type: ignore[import-not-found]
from sqlalchemy.engine import Connection

from runweave.core.settings import Settings
from runweave.storage.engine import postgres_engine


def migrate(connection: Connection) -> None:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def main() -> None:
    engine = postgres_engine(Settings())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(migrate)
    finally:
        await engine.dispose()


asyncio.run(main())
