"""Own each SQLite transaction on a fresh connection and reuse compiled statements."""

from contextvars import ContextVar
import os
import sqlite3
from functools import lru_cache
from typing import cast

from sqlalchemy import create_engine
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import NullPool

from robert_agent.persistence.dialects import sqlite
from robert_agent.persistence.errors import Error, translate_driver_errors


class DatabaseConnection:
    """A caller-owned connection whose context manages only the transaction."""

    def __init__(self, connection: Connection):
        # The dialect and statement executor share this handle inside persistence.
        self._connection = connection

    @property
    def _driver(self) -> sqlite3.Connection:
        return cast(sqlite3.Connection, self._connection.connection.driver_connection)

    @property
    def in_transaction(self) -> bool:
        with translate_driver_errors():
            if self._connection.invalidated:
                return False
            # SQLAlchemy's logical autobegin does not make a SELECT start a SQLite transaction.
            return self._driver.in_transaction

    def set_progress_handler(self, callback, instruction_count):
        """Preserve bounded observation and cancellation on this connection."""
        with translate_driver_errors():
            self._driver.set_progress_handler(callback, instruction_count)

    def commit(self) -> None:
        with translate_driver_errors():
            self._connection.commit()

    def rollback(self) -> None:
        with translate_driver_errors():
            self._connection.rollback()
            # An invalidated physical connection was discarded with its transaction.
            if self._connection.invalidated:
                return
            # A failed SQLAlchemy commit can end its logical transaction while
            # SQLite still has pending writes (for example a deferred FK failure).
            if self._driver.in_transaction:
                self._driver.rollback()

    def close(self) -> None:
        with translate_driver_errors():
            self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is not None:
            self.rollback()
            return
        try:
            self.commit()
        except Error:
            self.rollback()
            raise


def _database_target(db_path):
    filename = os.fspath(db_path)
    file_prefix = b"file:" if isinstance(filename, bytes) else "file:"
    # SQLite interprets URI escaping, modes and in-memory names itself.
    if filename.startswith(file_prefix) or not filename or filename in (":memory:", b":memory:"):
        return filename
    if os.path.isabs(filename):
        return filename
    # Prefix cwd without collapsing '..' across a possible symlink component.
    directory = os.getcwdb() if isinstance(filename, bytes) else os.getcwd()
    return os.path.join(directory, filename)


# Set only while engine.connect() opens the physical connection for one caller.
_CONNECT_TARGET: ContextVar = ContextVar("robert_sqlite_connect_target")


@lru_cache(maxsize=8)
def _engine_for(
    timeout,
    uri,
    isolation_level,
    foreign_keys,
    busy_timeout_ms,
    _process_id,
) -> Engine:
    """Share compiled statements across database files, with separate engines after a fork."""

    def open_driver():
        db_path = _CONNECT_TARGET.get(None)
        if db_path is None:
            # SQLAlchemy reconnects an invalidated connection outside connect(), where the file is unknown.
            raise sqlite3.ProgrammingError("Cannot reconnect an invalidated database connection.")
        options = {"timeout": timeout, "uri": uri, "isolation_level": isolation_level}
        if hasattr(sqlite3, "LEGACY_TRANSACTION_CONTROL"):
            options["autocommit"] = sqlite3.LEGACY_TRANSACTION_CONTROL
        driver = sqlite3.connect(db_path, **options)
        try:
            sqlite.configure(driver, foreign_keys=foreign_keys, busy_timeout_ms=busy_timeout_ms)
        except BaseException:
            driver.close()
            raise
        return driver

    # NullPool closes each physical connection at release, so cached engines hold no idle handles.
    return create_engine(
        "sqlite+pysqlite://",
        creator=open_driver,
        poolclass=NullPool,
        hide_parameters=True,
    )


def connect(
    db_path,
    *,
    timeout=5.0,
    uri=False,
    isolation_level="",
    foreign_keys=None,
    busy_timeout_ms=None,
) -> DatabaseConnection:
    """Open one physical connection with the existing sqlite3 access options."""
    engine = _engine_for(
        timeout,
        bool(uri),
        isolation_level,
        None if foreign_keys is None else bool(foreign_keys),
        None if busy_timeout_ms is None else int(busy_timeout_ms),
        os.getpid(),
    )
    token = _CONNECT_TARGET.set(_database_target(db_path))
    try:
        with translate_driver_errors():
            return DatabaseConnection(engine.connect())
    finally:
        _CONNECT_TARGET.reset(token)
