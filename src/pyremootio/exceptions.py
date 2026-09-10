"""Exceptions raised by the Remootio client."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyremootio.models import ActionResponse


class RemootioError(Exception):
    """Base error for the Remootio client."""


class RemootioConnectionError(RemootioError):
    """The websocket connection could not be established or was lost."""


class RemootioAuthenticationError(RemootioError):
    """The AUTH challenge/response flow failed."""


class RemootioCryptoError(RemootioError):
    """Encryption, decryption, or MAC verification failed."""


class RemootioTimeoutError(RemootioError):
    """A request did not complete within the expected time."""


class RemootioActionError(RemootioError):
    """The device rejected an action or reported success=false."""

    def __init__(self, response: ActionResponse) -> None:
        self.response = response
        message = response.error_code or f"{response.type} failed"
        super().__init__(message)
