"""SQLite database configuration for AutoLab.

The storage package deliberately accepts ordinary filesystem paths as well as
SQLAlchemy URLs.  This keeps the CLI pleasant while retaining an escape hatch
for tests and future database backends.
"""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


def _database_url(location: str | Path | URL) -> URL:
    if isinstance(location, URL):
        return location
    if isinstance(location, Path):
        return make_url(f"sqlite:///{location.expanduser().resolve()}")
    if "://" in location or location == "sqlite://":
        return make_url(location)
    return make_url(f"sqlite:///{Path(location).expanduser().resolve()}")


class Database:
    """Own the SQLAlchemy engine and its transaction/session policy."""

    def __init__(
        self,
        location: str | Path | URL = Path("data/autolab.sqlite3"),
        *,
        echo: bool = False,
    ) -> None:
        url = _database_url(location)
        kwargs: dict[str, Any] = {"echo": echo, "future": True}

        if url.get_backend_name() == "sqlite":
            kwargs["connect_args"] = {"check_same_thread": False}
            if url.database in (None, "", ":memory:"):
                kwargs["poolclass"] = StaticPool
            elif url.database:
                Path(url.database).parent.mkdir(parents=True, exist_ok=True)

        self.engine = create_engine(url, **kwargs)
        if url.get_backend_name() == "sqlite":
            event.listen(self.engine, "connect", _configure_sqlite)

        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            autoflush=False,
            expire_on_commit=False,
        )

    def create_schema(self) -> None:
        """Create the current schema for an isolated test database.

        Application initialization uses :meth:`Storage.initialize`, which
        applies the Alembic history and can safely upgrade an existing store.
        """

        from autonomous_lab.storage.orm import Base

        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Generator[Session, None, None]:
        """Yield a transaction that commits on success and rolls back on error."""

        session = self.session_factory()
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()


def _configure_sqlite(dbapi_connection: Any, _connection_record: Any) -> None:
    """Apply correctness and concurrency pragmas to every SQLite connection."""

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        # WAL is persistent at the database-file level and improves reader /
        # writer concurrency.  SQLite returns ``memory`` for in-memory DBs.
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
    finally:
        cursor.close()


def sqlite_pragmas(engine: Engine) -> dict[str, int | str]:
    """Return relevant pragma values; useful for diagnostics and tests."""

    with engine.connect() as connection:
        return {
            "foreign_keys": connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one(),
            "journal_mode": connection.exec_driver_sql("PRAGMA journal_mode").scalar_one(),
            "busy_timeout": connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one(),
        }
