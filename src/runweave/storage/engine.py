from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from runweave.core.settings import Settings


class StorageUnavailable(Exception):
    pass


@contextmanager
def storage_errors() -> Iterator[None]:
    try:
        yield
    except (SQLAlchemyError, OSError):
        raise StorageUnavailable("PostgreSQL operation failed") from None


def postgres_engine(settings: Settings) -> AsyncEngine:
    if settings.database_url is None:
        raise ValueError("Set RUNWEAVE_DATABASE_URL")
    try:
        url = make_url(settings.database_url.get_secret_value())
    except (ArgumentError, ValueError):
        raise ValueError("Invalid RUNWEAVE_DATABASE_URL") from None
    if url.drivername != "postgresql+asyncpg":
        raise ValueError("RUNWEAVE_DATABASE_URL must use postgresql+asyncpg")
    try:
        return create_async_engine(
            url,
            pool_size=settings.db_pool_size,
            max_overflow=0,
            pool_timeout=3,
            pool_pre_ping=True,
            hide_parameters=True,
            connect_args={"timeout": 3, "command_timeout": 3},
        )
    except ImportError:
        raise RuntimeError("Install requirements-durable.txt to enable PostgreSQL") from None
