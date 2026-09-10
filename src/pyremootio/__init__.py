"""Async Python client for the Remootio Websocket API."""

from pyremootio.client import RemootioClient
from pyremootio.exceptions import (
    RemootioActionError,
    RemootioAuthenticationError,
    RemootioConnectionError,
    RemootioCryptoError,
    RemootioError,
    RemootioTimeoutError,
)
from pyremootio.models import (
    ActionErrorCode,
    ActionResponse,
    ActionType,
    ConnectionVia,
    Credentials,
    DeviceErrorMessage,
    DoorState,
    EventType,
    KeyType,
    RemootioEvent,
    ServerHello,
)

__all__ = [
    "ActionErrorCode",
    "ActionResponse",
    "ActionType",
    "ConnectionVia",
    "Credentials",
    "DeviceErrorMessage",
    "DoorState",
    "EventType",
    "KeyType",
    "RemootioActionError",
    "RemootioAuthenticationError",
    "RemootioClient",
    "RemootioConnectionError",
    "RemootioCryptoError",
    "RemootioError",
    "RemootioEvent",
    "RemootioTimeoutError",
    "ServerHello",
]

__version__ = "0.1.0"
