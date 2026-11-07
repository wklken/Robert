"""Preserve the database failures that application code already distinguishes."""

from contextlib import contextmanager
from sqlite3 import DatabaseError as DatabaseError
from sqlite3 import Error as Error
from sqlite3 import IntegrityError as IntegrityError
from sqlite3 import OperationalError as OperationalError
from sqlite3 import ProgrammingError as ProgrammingError

from sqlalchemy.exc import DBAPIError, ResourceClosedError


@contextmanager
def translate_driver_errors():
    try:
        yield
    except DBAPIError as error:
        # Report the underlying failure without SQLAlchemy's statement and bindings.
        raise error.orig from None
    except ResourceClosedError:
        raise ProgrammingError("Cannot operate on a closed database.") from None
