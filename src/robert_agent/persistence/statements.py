"""Execute expressions inside persistence on the supplied connection."""

from collections.abc import Mapping
from contextlib import closing, contextmanager
import sqlite3

from sqlalchemy.dialects.sqlite import dialect

from robert_agent.persistence.database import DatabaseConnection
from robert_agent.persistence.errors import ProgrammingError, translate_driver_errors
from robert_agent.persistence.records import Record


def _bindings(parameters):
    if isinstance(parameters, Mapping):
        return dict(parameters)
    return {f"p{index}": value for index, value in enumerate(parameters)}


def _compiled_arguments(compiled, parameters):
    state = compiled.construct_expanded_state(parameters)
    return state.statement, state.positional_parameters


@contextmanager
def _borrowed_result(conn, stmt, parameters, *, many):
    # SQLite fixtures and legacy initialization can already own a transaction.
    # Compile on their physical connection without taking over its lifetime.
    sample = parameters[0] if many else parameters
    compiled = stmt.compile(dialect=dialect(paramstyle="qmark"), column_keys=list(sample))
    with closing(conn.cursor()) as cursor:
        cursor.row_factory = None
        if many:
            arguments = [_compiled_arguments(compiled, item) for item in parameters]
            sql = arguments[0][0]
            if any(statement != sql for statement, _ in arguments):
                raise ProgrammingError("Batch parameters changed the statement shape")
            cursor.executemany(sql, [values for _, values in arguments])
        else:
            sql, values = _compiled_arguments(compiled, parameters)
            cursor.execute(sql, values)
        yield cursor


@contextmanager
def _result(conn, stmt, parameters, *, many=False):
    if isinstance(conn, DatabaseConnection):
        with (
            translate_driver_errors(),
            closing(conn._connection.execute(stmt, parameters)) as result,
        ):
            yield result
    else:
        with _borrowed_result(conn, stmt, parameters, many=many) as result:
            yield result


def read(conn, stmt, parameters=()) -> list[Record]:
    with _result(conn, stmt, _bindings(parameters)) as result:
        if isinstance(result, sqlite3.Cursor):
            names = tuple(column[0] for column in result.description)
        else:
            names = tuple(result.keys())
        return [Record(names, tuple(row)) for row in result.fetchall()]


def write(conn, stmt, parameters=()) -> int:
    with _result(conn, stmt, _bindings(parameters)) as result:
        return result.rowcount


def write_many(conn, stmt, parameters) -> int:
    bindings = [_bindings(item) for item in parameters]
    if not bindings:
        return 0
    with _result(conn, stmt, bindings, many=True) as result:
        return result.rowcount
