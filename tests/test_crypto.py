"""Crypto tests using Websocket API v3 specification example vectors."""

from __future__ import annotations

import base64

import pytest

from pyremootio.crypto import decrypt_frame, encrypt_payload
from pyremootio.exceptions import RemootioCryptoError

SECRET_KEY = "EFD0E4BF75D49BDD4F5CD5492D55C92FE96040E9CD74BED9F19ACA2658EA0FA9"
AUTH_KEY = "7B456E7AE95E55F714E2270983C33360514DAD96C93AE1990AFE35FD5BF00A72"
SESSION_KEY = "yzEI7RWCjYDEwFrgc5YrmWo82kXEjFNStbtN+wFM2Qk="

CHALLENGE_FRAME = {
    "type": "ENCRYPTED",
    "data": {
        "iv": "4kbmkg6iU29Zlpi3NCDM4g==",
        "payload": (
            "ZTQwhEWXMV2ZxkzDJiJWyCD52FF88pha8lJbpD2KYk5B6TGQvBaTJlA7apd+lO38"
            "mu44NA7heNVZOc6B6jVwqvdqMSrEdV33KgaHMZY7yNXBq4aP3+Z2ai4TJ8Smgnj6"
            "Z77J4qeT6MqBbr0FTLYkEg=="
        ),
    },
    "mac": "qko4r2/Eucwh8FqJIXucKn/w/ftR9+vs05E8A1/y++Q=",
}

QUERY_FRAME = {
    "type": "ENCRYPTED",
    "data": {
        "iv": "vz3r424R6v9XFchkkgWQTw==",
        "payload": "L6eTyvyY/q4I7oDAfdeDyz17x0vMUqmqvnCYl73zG2UxnYpIKVIQ0DooAWxcm3WT",
    },
    "mac": "legB+2ZnikMtX54VpkPVc8P7o17s61y1JqGDvFrxbts=",
}

QUERY_RESPONSE_FRAME = {
    "type": "ENCRYPTED",
    "data": {
        "iv": "S7Mt0PR3MCADhHOPqhJPLA==",
        "payload": (
            "pSw+jH9iR3/nOO2+78EpQct3w+vJGKku+8ynSaYra6WsU4dHQJfMg1KNJkooVb1/"
            "WYhT28NyGznEHEKt97SYTMG15KjWcQUuqRSlpGD3JzWi/5LG+JPvIg3ptivsFrRZ"
            "R3wzHAtZI6CekFujm8dhjeK/o6w+daK4FdvVh78pVigX6tBuNHEjoRQfUL9TRS9W"
        ),
    },
    "mac": "cD4IpRARmeWoUjkL4Kh40uhOMbs7P9prP497qZUapwQ=",
}


def test_decrypt_auth_challenge() -> None:
    payload = decrypt_frame(
        CHALLENGE_FRAME,
        auth_key=AUTH_KEY,
        secret_key=SECRET_KEY,
    )
    assert payload["challenge"]["sessionKey"] == SESSION_KEY
    assert payload["challenge"]["initialActionId"] == 808411243


def test_decrypt_query_action_with_session_key() -> None:
    payload = decrypt_frame(
        QUERY_FRAME,
        auth_key=AUTH_KEY,
        session_key=SESSION_KEY,
    )
    # Spec prose shows id=808411243; the published ciphertext is initialActionId+1.
    assert payload == {"action": {"type": "QUERY", "id": 808411244}}


def test_encrypt_query_matches_spec_vector() -> None:
    iv = base64.b64decode("vz3r424R6v9XFchkkgWQTw==")
    frame = encrypt_payload(
        {"action": {"type": "QUERY", "id": 808411244}},
        auth_key=AUTH_KEY,
        session_key=SESSION_KEY,
        iv=iv,
    )
    assert frame == QUERY_FRAME


def test_decrypt_query_response() -> None:
    payload = decrypt_frame(
        QUERY_RESPONSE_FRAME,
        auth_key=AUTH_KEY,
        session_key=SESSION_KEY,
    )
    assert payload["response"]["type"] == "QUERY"
    assert payload["response"]["id"] == 808411244
    assert payload["response"]["success"] is True
    assert payload["response"]["state"] == "no sensor"
    assert payload["response"]["t100ms"] == 8985
    assert payload["response"]["relayTriggered"] is False
    assert payload["response"]["errorCode"] == ""


def test_bad_mac_raises() -> None:
    frame = {
        **CHALLENGE_FRAME,
        "mac": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    }
    with pytest.raises(RemootioCryptoError, match="MAC"):
        decrypt_frame(frame, auth_key=AUTH_KEY, secret_key=SECRET_KEY)


def test_encrypt_decrypt_roundtrip() -> None:
    original = {"action": {"type": "OPEN", "id": 42, "duration": 5}}
    frame = encrypt_payload(original, auth_key=AUTH_KEY, session_key=SESSION_KEY)
    assert decrypt_frame(frame, auth_key=AUTH_KEY, session_key=SESSION_KEY) == original


@pytest.mark.parametrize(
    "mac",
    [
        "short",
        "A" * 8,
        "@@@not-valid-base64@@@",
        "é" * 44,
    ],
)
def test_malformed_mac_raises(mac: str) -> None:
    frame = {**CHALLENGE_FRAME, "mac": mac}
    with pytest.raises(RemootioCryptoError, match="MAC"):
        decrypt_frame(frame, auth_key=AUTH_KEY, secret_key=SECRET_KEY)


def test_session_key_must_be_32_bytes() -> None:
    short_key = base64.b64encode(b"too-short").decode("ascii")
    with pytest.raises(RemootioCryptoError, match="session_key must be 32 bytes"):
        encrypt_payload(
            {"action": {"type": "QUERY", "id": 1}},
            auth_key=AUTH_KEY,
            session_key=short_key,
        )


def test_session_key_must_be_valid_base64() -> None:
    with pytest.raises(RemootioCryptoError, match="session_key is not valid base64"):
        encrypt_payload(
            {"action": {"type": "QUERY", "id": 1}},
            auth_key=AUTH_KEY,
            session_key="@@@",
        )


def test_secret_key_must_be_32_bytes() -> None:
    with pytest.raises(RemootioCryptoError, match="secret_key must be 32 bytes"):
        encrypt_payload(
            {"action": {"type": "QUERY", "id": 1}},
            auth_key=AUTH_KEY,
            secret_key="aa",
        )


def test_session_key_rejects_oversized_key() -> None:
    oversized = base64.b64encode(bytes(64)).decode("ascii")
    with pytest.raises(RemootioCryptoError, match="session_key must be 32 bytes"):
        encrypt_payload(
            {"action": {"type": "QUERY", "id": 1}},
            auth_key=AUTH_KEY,
            session_key=oversized,
        )


def test_secret_key_must_be_valid_hex() -> None:
    with pytest.raises(RemootioCryptoError, match="secret_key is not valid hex"):
        encrypt_payload(
            {"action": {"type": "QUERY", "id": 1}},
            auth_key=AUTH_KEY,
            secret_key="zzzz",
        )
