"""Optional HTTP Basic authentication for the Web request boundary."""

import base64
import binascii
from getpass import GetPassWarning, getpass
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import stat
import warnings


ITERATIONS = 600_000
MAX_FILE_BYTES = 16_384
MAX_CREDENTIAL_BYTES = 4096


def _valid_username(username):
    return (
        isinstance(username, str)
        and bool(username)
        and ":" not in username
        and not any(ord(character) < 32 or ord(character) == 127 for character in username)
        and len(username.encode("utf-8")) <= MAX_CREDENTIAL_BYTES
    )


@dataclass(frozen=True)
class BasicAuth:
    username: str
    salt: bytes = field(repr=False)
    password_hash: bytes = field(repr=False)
    iterations: int

    def authorized(self, authorization):
        if not isinstance(authorization, str) or len(authorization) > MAX_FILE_BYTES:
            return False
        scheme, separator, token = authorization.partition(" ")
        if not separator or scheme.lower() != "basic":
            return False
        try:
            decoded = base64.b64decode(token, validate=True)
            if len(decoded) > MAX_CREDENTIAL_BYTES:
                return False
            username, separator, password = decoded.decode("utf-8").partition(":")
        except (ValueError, UnicodeError, binascii.Error):
            return False
        if not separator:
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), self.salt, self.iterations)
        user_matches = hmac.compare_digest(username.encode("utf-8"), self.username.encode("utf-8"))
        return hmac.compare_digest(candidate, self.password_hash) and user_matches


def write_basic_auth_file(path, username, password):
    """Create a private salted password verifier without overwriting a file."""
    if not _valid_username(username) or not isinstance(password, str) or not password:
        raise ValueError(
            "Basic Auth requires a non-empty username without colons or control characters and a password."
        )
    if len(f"{username}:{password}".encode("utf-8")) > MAX_CREDENTIAL_BYTES:
        raise ValueError("Basic Auth credentials exceed the supported length.")
    salt = secrets.token_bytes(32)
    record = {
        "version": 1,
        "username": username,
        "algorithm": "pbkdf2-sha256",
        "iterations": ITERATIONS,
        "salt": salt.hex(),
        "password_hash": hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, ITERATIONS).hex(),
    }
    descriptor = os.open(Path(path).expanduser(), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(record, stream)
        stream.write("\n")


def load_basic_auth_file(path):
    """Reject an invalid or publicly readable verifier before opening a listener."""
    error = "Basic Auth file must be a private regular file containing a valid password verifier."
    try:
        descriptor = os.open(Path(path).expanduser(), os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            metadata = os.fstat(stream.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid():
                raise ValueError(error)
            content = stream.read(MAX_FILE_BYTES + 1)
        if len(content) > MAX_FILE_BYTES:
            raise ValueError(error)
        record = json.loads(content)
        if (
            not isinstance(record, dict)
            or record.get("version") != 1
            or record.get("algorithm") != "pbkdf2-sha256"
            or not _valid_username(record.get("username"))
            or type(record.get("iterations")) is not int
            or not ITERATIONS <= record["iterations"] <= 2_000_000
        ):
            raise ValueError(error)
        salt = bytes.fromhex(record["salt"])
        password_hash = bytes.fromhex(record["password_hash"])
        if len(salt) != 32 or len(password_hash) != 32:
            raise ValueError(error)
        return BasicAuth(record["username"], salt, password_hash, record["iterations"])
    except (OSError, ValueError, KeyError, TypeError, UnicodeError, RecursionError) as exc:
        raise ValueError(error) from exc


def initialize_basic_auth_file(path, username):
    """Prompt on the terminal so passwords never enter command arguments or output."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", GetPassWarning)
            password = getpass("Basic Auth password: ")
            if password != getpass("Confirm password: "):
                raise ValueError("Basic Auth passwords did not match.")
        write_basic_auth_file(path, username, password)
    except GetPassWarning:
        return {
            "ok": False,
            "status": "failed_input",
            "safe_error": "Basic Auth password input requires a terminal with echo disabled.",
        }
    except (OSError, ValueError, EOFError) as exc:
        error = str(exc) if isinstance(exc, ValueError) else "Unable to create Basic Auth file."
        return {"ok": False, "status": "failed_input", "safe_error": error}
    return {"ok": True, "status": "created", "username": username, "auth_file": str(Path(path).expanduser())}
