"""Hash a password for AGENDA_PASSWORD_HASH (optional login in the agenda app).

    docker compose run --rm --no-deps web python -m calapp.web.hashpw
"""

from __future__ import annotations

import getpass
import hashlib
import hmac
import secrets

ITERATIONS = 600_000


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${digest.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, iterations, salt, digest = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        given = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except ValueError:
        return False
    return hmac.compare_digest(given.hex(), digest)


if __name__ == "__main__":
    first = getpass.getpass("Password: ")
    if len(first) < 12:
        raise SystemExit("use at least 12 characters")
    if getpass.getpass("Repeat: ") != first:
        raise SystemExit("passwords differ")
    print(f"AGENDA_PASSWORD_HASH={hash_password(first)}")
