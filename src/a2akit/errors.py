"""Framework-level exceptions for a2akit."""

from __future__ import annotations


class AuthenticationRequiredError(Exception):
    """Raised by auth middleware when credentials are missing or invalid.

    The server exception handler returns HTTP 401 with a
    ``WWW-Authenticate`` header built from *scheme* and *realm*.

    Usage::

        raise AuthenticationRequiredError(scheme="Bearer", realm="a2a")
    """

    def __init__(self, scheme: str = "Bearer", realm: str = "a2a") -> None:
        self.scheme = scheme
        self.realm = realm
        super().__init__(f"{scheme} authentication required")
