"""AES-256-CBC + HMAC-SHA256 helpers for Remootio ENCRYPTED frames."""

from __future__ import annotations

import base64
import hmac
import json
from collections.abc import Mapping
from hashlib import sha256
from os import urandom
from typing import Any

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

from pyremootio.exceptions import RemootioCryptoError

_AES_BLOCK_BITS = 128
_AES_KEY_LENGTH = 32
_IV_LENGTH = 16
_JSON_SEPARATORS = (",", ":")


def compact_json(value: Mapping[str, Any] | list[Any] | str | int | bool | None) -> str:
    """Serialize JSON without whitespace, matching the device MAC input."""
    return json.dumps(value, separators=_JSON_SEPARATORS)


def _b64decode(value: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, UnicodeError) as err:
        raise RemootioCryptoError("Invalid base64") from err


def _require_aes_key(key: bytes, *, name: str) -> bytes:
    if len(key) != _AES_KEY_LENGTH:
        raise RemootioCryptoError(f"{name} must be {_AES_KEY_LENGTH} bytes")
    return key


def _encryption_key(*, secret_key: str | None, session_key: str | None) -> bytes:
    if session_key is not None:
        try:
            key = _b64decode(session_key)
        except RemootioCryptoError as err:
            raise RemootioCryptoError("session_key is not valid base64") from err
        return _require_aes_key(key, name="session_key")
    if secret_key is None:
        raise RemootioCryptoError("An encryption key is required")
    try:
        key = bytes.fromhex(secret_key)
    except ValueError as err:
        raise RemootioCryptoError("secret_key is not valid hex") from err
    return _require_aes_key(key, name="secret_key")


def _auth_key_bytes(auth_key: str) -> bytes:
    try:
        return bytes.fromhex(auth_key)
    except ValueError as err:
        raise RemootioCryptoError("auth_key is not valid hex") from err


def _mac_digest(data: Mapping[str, str], auth_key: str) -> bytes:
    mac_input = compact_json({"iv": data["iv"], "payload": data["payload"]})
    return hmac.new(_auth_key_bytes(auth_key), mac_input.encode("latin-1"), sha256).digest()


def _mac_for_data(data: Mapping[str, str], auth_key: str) -> str:
    return base64.b64encode(_mac_digest(data, auth_key)).decode("ascii")


def _verify_mac(data: Mapping[str, str], auth_key: str, mac: str) -> None:
    """Compare HMAC digests in constant time, even if ``mac`` is malformed."""
    expected = _mac_digest(data, auth_key)
    received: bytes | None
    try:
        received = base64.b64decode(mac, validate=True)
    except (ValueError, UnicodeError):
        received = None
    if received is None or len(received) != len(expected):
        candidate = bytes(len(expected))
        valid = False
    else:
        candidate = received
        valid = True
    if not hmac.compare_digest(expected, candidate) or not valid:
        raise RemootioCryptoError("ENCRYPTED frame MAC does not match")


def decrypt_frame(
    frame: Mapping[str, Any],
    *,
    auth_key: str,
    secret_key: str | None = None,
    session_key: str | None = None,
) -> dict[str, Any]:
    """Decrypt an ENCRYPTED frame and verify its HMAC.

    Use ``secret_key`` before the session is authenticated (AUTH challenge).
    Use ``session_key`` for every later ENCRYPTED frame.
    """
    if frame.get("type") != "ENCRYPTED":
        raise RemootioCryptoError("Frame is not an ENCRYPTED frame")
    data = frame.get("data")
    mac = frame.get("mac")
    if not isinstance(data, Mapping) or not isinstance(mac, str):
        raise RemootioCryptoError("ENCRYPTED frame is missing data or mac")
    iv_b64 = data.get("iv")
    payload_b64 = data.get("payload")
    if not isinstance(iv_b64, str) or not isinstance(payload_b64, str):
        raise RemootioCryptoError("ENCRYPTED frame data is invalid")

    _verify_mac({"iv": iv_b64, "payload": payload_b64}, auth_key, mac)

    key = _encryption_key(secret_key=secret_key, session_key=session_key)
    try:
        iv = _b64decode(iv_b64)
        if len(iv) != _IV_LENGTH:
            raise RemootioCryptoError("IV must be 16 bytes")
        ciphertext = _b64decode(payload_b64)
        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        padded = decryptor.update(ciphertext) + decryptor.finalize()
        unpadder = PKCS7(_AES_BLOCK_BITS).unpadder()
        plaintext = unpadder.update(padded) + unpadder.finalize()
        decoded = plaintext.decode("latin-1")
        parsed = json.loads(decoded)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as err:
        raise RemootioCryptoError("Failed to decrypt ENCRYPTED frame") from err
    if not isinstance(parsed, dict):
        raise RemootioCryptoError("Decrypted payload is not a JSON object")
    return parsed


def encrypt_payload(
    payload: Mapping[str, Any],
    *,
    auth_key: str,
    session_key: str | None = None,
    secret_key: str | None = None,
    iv: bytes | None = None,
) -> dict[str, Any]:
    """Wrap an unencrypted payload in an ENCRYPTED frame.

    The device encrypts the AUTH challenge with ``secret_key``. Every later
    frame uses ``session_key``.
    """
    if iv is None:
        iv = urandom(_IV_LENGTH)
    elif len(iv) != _IV_LENGTH:
        raise RemootioCryptoError("IV must be 16 bytes")

    key = _encryption_key(secret_key=secret_key, session_key=session_key)
    plaintext = compact_json(payload).encode("latin-1")
    padder = PKCS7(_AES_BLOCK_BITS).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    data = {
        "iv": base64.b64encode(iv).decode("ascii"),
        "payload": base64.b64encode(ciphertext).decode("ascii"),
    }
    return {
        "type": "ENCRYPTED",
        "data": data,
        "mac": _mac_for_data(data, auth_key),
    }
