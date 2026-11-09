"""SQLite connection settings and catalog operations, never business policy."""

from robert_agent.persistence import errors
from robert_agent.foundation.timestamps import timestamp_after


def execute_control(conn, sql):
    """Execute internally constructed transaction/schema control on its owner."""
    with errors.translate_driver_errors():
        if hasattr(conn, "_connection"):
            result = conn._connection.exec_driver_sql(sql)
            try:
                return [tuple(row) for row in result] if result.returns_rows else []
            finally:
                result.close()
        cursor = conn.execute(sql)
        try:
            return [tuple(row) for row in cursor] if cursor.description else []
        finally:
            cursor.close()


def configure(conn, *, foreign_keys=None, busy_timeout_ms=None, query_only=None):
    if foreign_keys is not None:
        execute_control(conn, f"PRAGMA foreign_keys={int(bool(foreign_keys))}")
    if busy_timeout_ms is not None:
        execute_control(conn, f"PRAGMA busy_timeout={int(busy_timeout_ms)}")
    if query_only is not None:
        execute_control(conn, f"PRAGMA query_only={int(bool(query_only))}")


def table_info(conn, name):
    name = name.replace('"', '""')
    return execute_control(conn, f'PRAGMA table_info("{name}")')


def database_list(conn):
    return execute_control(conn, "PRAGMA database_list")


def page_count(conn):
    return execute_control(conn, "PRAGMA page_count")[0][0]


def freelist_count(conn):
    return execute_control(conn, "PRAGMA freelist_count")[0][0]


def page_size(conn):
    return execute_control(conn, "PRAGMA page_size")[0][0]


def register_unicode_lower(conn):
    """Keep Unicode case conversion identical to Python-side text filters."""
    driver = conn._driver if hasattr(conn, "_driver") else conn
    with errors.translate_driver_errors():
        driver.create_function("python_lower", 1, lambda value: str(value or "").lower(), deterministic=True)


def register_timestamp_after(conn):
    """Retain exact timezone and microsecond comparison for delivery boundaries."""
    driver = conn._driver if hasattr(conn, "_driver") else conn
    with errors.translate_driver_errors():
        driver.create_function("python_timestamp_after", 2, timestamp_after, deterministic=True)
