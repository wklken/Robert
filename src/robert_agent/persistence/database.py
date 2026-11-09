"""Own each SQLite transaction on a fresh connection and reuse compiled statements."""

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
    if filename.startswith(file_prefix):
        # SQLite interprets URI escaping, modes and in-memory names itself.
        # Scope relative URI cache keys to the working directory.
        uri_path = filename[len(file_prefix) :]
        return filename, None if os.path.isabs(uri_path) else os.getcwd()
    if not filename or filename in (":memory:", b":memory:") or os.path.isabs(filename):
        return filename, None
    # Prefix cwd without collapsing '..' across a possible symlink component.
    directory = os.getcwdb() if isinstance(filename, bytes) else os.getcwd()
    return os.path.join(directory, filename), None


@lru_cache(maxsize=8)
def _engine_for(
    db_path,
    timeout,
    uri,
    isolation_level,
    foreign_keys,
    busy_timeout_ms,
    _process_id,
    _relative_uri_cwd,
) -> Engine:
    """Keep only engine and compiler state, with separate cache keys after a fork."""

    def open_driver():
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
    filename, relative_uri_cwd = _database_target(db_path)
    engine = _engine_for(
        filename,
        timeout,
        bool(uri),
        isolation_level,
        None if foreign_keys is None else bool(foreign_keys),
        None if busy_timeout_ms is None else int(busy_timeout_ms),
        os.getpid(),
        relative_uri_cwd,
    )
    with translate_driver_errors():
        return DatabaseConnection(engine.connect())
