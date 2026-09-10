"""Typed models for Remootio Websocket API frames, actions, and events."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pyremootio.const import HEX_KEY_LENGTH

_HEX_KEY_RE = re.compile(rf"^[0-9A-Fa-f]{{{HEX_KEY_LENGTH}}}$")


class DoorState(StrEnum):
    """Gate / garage door sensor state reported by the device."""

    OPEN = "open"
    CLOSED = "closed"
    NO_SENSOR = "no sensor"
    UNKNOWN = "unknown"


class ActionType(StrEnum):
    """Encrypted action types the client can send."""

    QUERY = "QUERY"
    TRIGGER = "TRIGGER"
    TRIGGER_SECONDARY = "TRIGGER_SECONDARY"
    OPEN = "OPEN"
    CLOSE = "CLOSE"
    RESTART = "RESTART"


class EventType(StrEnum):
    """Device event types (logging mode determines which are emitted)."""

    STATE_CHANGE = "StateChange"
    RELAY_TRIGGER = "RelayTrigger"
    SECONDARY_RELAY_TRIGGER = "SecondaryRelayTrigger"
    OUTPUT_HELD_ACTIVE = "OutputHeldActive"
    SECONDARY_OUTPUT_HELD_ACTIVE = "SecondaryOutputHeldActive"
    CONNECTED = "Connected"
    LEFT_OPEN = "LeftOpen"
    KEY_MANAGEMENT = "KeyManagement"
    RESTART = "Restart"
    MANUAL_BUTTON_PUSHED = "ManualButtonPushed"
    MANUAL_BUTTON_ENABLED = "ManualButtonEnabled"
    MANUAL_BUTTON_DISABLED = "ManualButtonDisabled"
    DOORBELL_PUSHED = "DoorbellPushed"
    DOORBELL_ENABLED = "DoorbellEnabled"
    DOORBELL_DISABLED = "DoorbellDisabled"
    SENSOR_ENABLED = "SensorEnabled"
    SENSOR_FLIPPED = "SensorFlipped"
    SENSOR_DISABLED = "SensorDisabled"
    OUTPUT1_ACTIVATED = "Output1Activated"
    OUTPUT1_DEACTIVATED = "Output1Deactivated"
    OUTPUT2_ACTIVATED = "Output2Activated"
    OUTPUT2_DEACTIVATED = "Output2Deactivated"


class KeyType(StrEnum):
    """Key class reported in event data."""

    MASTER_KEY = "master key"
    UNIQUE_KEY = "unique key"
    GUEST_KEY = "guest key"
    API_KEY = "api key"
    SMART_HOME = "smart home"
    AUTOMATION = "automation"


class ConnectionVia(StrEnum):
    """How a key reached the device for an event."""

    BLUETOOTH = "bluetooth"
    WIFI = "wifi"
    INTERNET = "internet"
    AUTOOPEN = "autoopen"
    UNKNOWN = "unknown"
    NONE = "none"


class DeviceErrorMessage(StrEnum):
    """ERROR frame errorMessage values sent by the device."""

    JSON_ERROR = "json error"
    INPUT_ERROR = "input error"
    INTERNAL_ERROR = "internal error"
    CONNECTION_TIMEOUT = "connection timeout"
    AUTHENTICATION_TIMEOUT = "authentication timeout"
    ALREADY_AUTHENTICATED = "already authenticated"
    AUTHENTICATION_ERROR = "authentication error"


class ActionErrorCode(StrEnum):
    """errorCode values in action responses."""

    NONE = ""
    RELAY_BUSY = "ERR_RELAY_BUSY"
    NO_SENSOR = "ERR_NO_SENSOR"
    INVALID_REQUEST = "ERR_INVALID_REQUEST"


def _parse_door_state(value: object) -> DoorState:
    if isinstance(value, DoorState):
        return value
    if isinstance(value, str):
        try:
            return DoorState(value)
        except ValueError:
            return DoorState.UNKNOWN
    return DoorState.UNKNOWN


@dataclass(frozen=True, slots=True)
class Credentials:
    """API Secret Key and API Auth Key from the Remootio app."""

    secret_key: str
    auth_key: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "secret_key", self.secret_key.strip())
        object.__setattr__(self, "auth_key", self.auth_key.strip())
        if not _HEX_KEY_RE.fullmatch(self.secret_key):
            raise ValueError("secret_key must be a 64-character hex string")
        if not _HEX_KEY_RE.fullmatch(self.auth_key):
            raise ValueError("auth_key must be a 64-character hex string")

    def __repr__(self) -> str:
        return "Credentials(secret_key='***', auth_key='***')"


@dataclass(frozen=True, slots=True)
class ServerHello:
    """Identity returned by a HELLO / SERVER_HELLO exchange."""

    api_version: int
    message: str
    serial_number: str | None = None
    remootio_version: str | None = None

    @classmethod
    def from_frame(cls, frame: Mapping[str, Any]) -> ServerHello:
        api_version = frame.get("apiVersion", 1)
        try:
            parsed_version = int(api_version)
        except (TypeError, ValueError):
            parsed_version = 1
        serial = frame.get("serialNumber")
        version = frame.get("remootioVersion")
        return cls(
            api_version=parsed_version,
            message=str(frame.get("message", "")),
            serial_number=str(serial) if serial else None,
            remootio_version=str(version) if version else None,
        )


@dataclass(frozen=True, slots=True)
class Challenge:
    """AUTH challenge payload: session key plus the starting action counter."""

    session_key: str
    initial_action_id: int

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Challenge:
        challenge = payload.get("challenge")
        if not isinstance(challenge, Mapping):
            raise ValueError("AUTH challenge payload is missing challenge")
        session_key = challenge.get("sessionKey")
        initial_action_id = challenge.get("initialActionId")
        if not isinstance(session_key, str) or not isinstance(initial_action_id, int):
            raise ValueError("AUTH challenge payload is invalid")
        return cls(session_key=session_key, initial_action_id=initial_action_id)


@dataclass(frozen=True, slots=True)
class ActionResponse:
    """Decrypted response to an encrypted action."""

    type: ActionType
    id: int
    success: bool
    state: DoorState
    t100ms: int
    relay_triggered: bool
    error_code: str

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> ActionResponse:
        response = payload.get("response")
        if not isinstance(response, Mapping):
            raise ValueError("Action response payload is missing response")
        raw_type = str(response.get("type", ""))
        try:
            action_type = ActionType(raw_type)
        except ValueError as err:
            raise ValueError(f"Unknown action response type: {raw_type}") from err
        return cls(
            type=action_type,
            id=int(response.get("id", 0)),
            success=bool(response.get("success", False)),
            state=_parse_door_state(response.get("state")),
            t100ms=int(response.get("t100ms", 0)),
            relay_triggered=bool(response.get("relayTriggered", False)),
            error_code=str(response.get("errorCode", "")),
        )


@dataclass(frozen=True, slots=True)
class RemootioEvent:
    """Decrypted event pushed by the device."""

    type: str
    state: DoorState
    cnt: int
    t100ms: int
    data: dict[str, Any] | None = None

    @property
    def event_type(self) -> EventType | None:
        """Known EventType, or None if the device sent an unrecognized type."""
        try:
            return EventType(self.type)
        except ValueError:
            return None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> RemootioEvent:
        event = payload.get("event")
        if not isinstance(event, Mapping):
            raise ValueError("Event payload is missing event")
        data = event.get("data")
        return cls(
            type=str(event.get("type", "")),
            state=_parse_door_state(event.get("state")),
            cnt=int(event.get("cnt", 0)),
            t100ms=int(event.get("t100ms", 0)),
            data=dict(data) if isinstance(data, Mapping) else None,
        )
