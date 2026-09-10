"""Model parsing and credentials validation tests."""

from __future__ import annotations

import pytest

from pyremootio.models import (
    ActionResponse,
    ActionType,
    Credentials,
    DoorState,
    EventType,
    RemootioEvent,
    ServerHello,
)


def test_credentials_require_64_char_hex() -> None:
    with pytest.raises(ValueError, match="secret_key"):
        Credentials(secret_key="not-hex", auth_key="A" * 64)
    with pytest.raises(ValueError, match="auth_key"):
        Credentials(secret_key="A" * 64, auth_key="xyz")
    creds = Credentials(secret_key="ab" * 32, auth_key="CD" * 32)
    assert creds.secret_key == "ab" * 32
    assert creds.auth_key == "CD" * 32
    assert "ab" not in repr(creds)
    assert "CD" not in repr(creds)
    assert "***" in repr(creds)


def test_server_hello_v3() -> None:
    hello = ServerHello.from_frame(
        {
            "type": "SERVER_HELLO",
            "apiVersion": 3,
            "message": "This is the Remootio Websocket API",
            "serialNumber": "abc123",
            "remootioVersion": "remootio-3",
        }
    )
    assert hello.api_version == 3
    assert hello.serial_number == "abc123"
    assert hello.remootio_version == "remootio-3"


def test_server_hello_v1_has_no_serial() -> None:
    hello = ServerHello.from_frame({"type": "SERVER_HELLO", "apiVersion": 1, "message": "hello"})
    assert hello.api_version == 1
    assert hello.serial_number is None


def test_action_response_and_event_parsing() -> None:
    response = ActionResponse.from_payload(
        {
            "response": {
                "type": "OPEN",
                "id": 9,
                "success": True,
                "state": "open",
                "t100ms": 10,
                "relayTriggered": True,
                "errorCode": "",
            }
        }
    )
    assert response.type is ActionType.OPEN
    assert response.state is DoorState.OPEN

    event = RemootioEvent.from_payload(
        {
            "event": {
                "cnt": 1,
                "type": "StateChange",
                "state": "closed",
                "t100ms": 20,
            }
        }
    )
    assert event.event_type is EventType.STATE_CHANGE
    assert event.state is DoorState.CLOSED


def test_unknown_event_type_is_preserved() -> None:
    event = RemootioEvent.from_payload(
        {"event": {"cnt": 1, "type": "FutureEvent", "state": "open", "t100ms": 1}}
    )
    assert event.type == "FutureEvent"
    assert event.event_type is None


def test_unknown_door_state_becomes_unknown() -> None:
    event = RemootioEvent.from_payload(
        {"event": {"cnt": 1, "type": "StateChange", "state": "ajar", "t100ms": 1}}
    )
    assert event.state is DoorState.UNKNOWN
