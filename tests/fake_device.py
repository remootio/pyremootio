"""In-process Remootio Websocket API fake used by client tests."""

from __future__ import annotations

import json
from typing import Any

from aiohttp import WSMsgType, web

from pyremootio.crypto import decrypt_frame, encrypt_payload

SPEC_SECRET_KEY = "EFD0E4BF75D49BDD4F5CD5492D55C92FE96040E9CD74BED9F19ACA2658EA0FA9"
SPEC_AUTH_KEY = "7B456E7AE95E55F714E2270983C33360514DAD96C93AE1990AFE35FD5BF00A72"
SPEC_SESSION_KEY = "yzEI7RWCjYDEwFrgc5YrmWo82kXEjFNStbtN+wFM2Qk="
SPEC_INITIAL_ACTION_ID = 808411243


class FakeRemootioDevice:
    """Minimal device-side protocol implementation for tests."""

    def __init__(
        self,
        *,
        secret_key: str = SPEC_SECRET_KEY,
        auth_key: str = SPEC_AUTH_KEY,
        session_key: str = SPEC_SESSION_KEY,
        initial_action_id: int = SPEC_INITIAL_ACTION_ID,
        serial_number: str = "2462abe6bda0nfmcfaxm",
        remootio_version: str = "remootio-3",
        api_version: int = 3,
        door_state: str = "closed",
        fail_auth: bool = False,
        action_error: str | None = None,
    ) -> None:
        self.secret_key = secret_key
        self.auth_key = auth_key
        self.session_key = session_key
        self.initial_action_id = initial_action_id
        self.serial_number = serial_number
        self.remootio_version = remootio_version
        self.api_version = api_version
        self.door_state = door_state
        self.fail_auth = fail_auth
        self.action_error = action_error
        self.received: list[dict[str, Any]] = []
        self._ws: web.WebSocketResponse | None = None
        self._authenticated = False
        self._expected_action_id = (initial_action_id + 1) % 0x7FFFFFFF

    def _reset_connection(self) -> None:
        self._authenticated = False
        self._expected_action_id = (self.initial_action_id + 1) % 0x7FFFFFFF

    async def websocket_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        self._reset_connection()
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            frame = json.loads(message.data)
            self.received.append(frame)
            await self._handle_frame(ws, frame)
        self._ws = None
        return ws

    async def push_event(self, event: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise RuntimeError("Fake device is not connected")
        frame = encrypt_payload(
            {"event": event},
            auth_key=self.auth_key,
            session_key=self.session_key,
        )
        await self._ws.send_str(json.dumps(frame, separators=(",", ":")))

    async def _handle_frame(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        frame_type = frame.get("type")
        if frame_type == "HELLO":
            await ws.send_str(
                json.dumps(
                    {
                        "type": "SERVER_HELLO",
                        "apiVersion": self.api_version,
                        "message": "This is the Remootio Websocket API",
                        "serialNumber": self.serial_number,
                        "remootioVersion": self.remootio_version,
                    },
                    separators=(",", ":"),
                )
            )
            return
        if frame_type == "PING":
            await ws.send_str(json.dumps({"type": "PONG"}, separators=(",", ":")))
            return
        if frame_type == "AUTH":
            if self.fail_auth:
                await ws.send_str(
                    json.dumps(
                        {"type": "ERROR", "errorMessage": "authentication error"},
                        separators=(",", ":"),
                    )
                )
                await ws.close()
                return
            challenge = encrypt_payload(
                {
                    "challenge": {
                        "sessionKey": self.session_key,
                        "initialActionId": self.initial_action_id,
                    }
                },
                auth_key=self.auth_key,
                secret_key=self.secret_key,
            )
            await ws.send_str(json.dumps(challenge, separators=(",", ":")))
            return
        if frame_type == "ENCRYPTED":
            payload = decrypt_frame(
                frame,
                auth_key=self.auth_key,
                session_key=self.session_key,
            )
            action = payload.get("action", {})
            action_type = action.get("type", "QUERY")
            action_id = int(action.get("id", 0))
            success = self.action_error is None
            if action_id != self._expected_action_id:
                await ws.send_str(
                    json.dumps(
                        {"type": "ERROR", "errorMessage": "authentication error"},
                        separators=(",", ":"),
                    )
                )
                await ws.close()
                return
            self._expected_action_id = (action_id + 1) % 0x7FFFFFFF
            self._authenticated = True
            if action_type == "OPEN" and self.door_state == "closed" and success:
                self.door_state = "open"
            if action_type == "CLOSE" and self.door_state == "open" and success:
                self.door_state = "closed"
            response = encrypt_payload(
                {
                    "response": {
                        "type": action_type,
                        "id": action_id,
                        "success": success,
                        "state": self.door_state,
                        "t100ms": 100,
                        "relayTriggered": action_type in {"TRIGGER", "OPEN", "CLOSE"} and success,
                        "errorCode": self.action_error or "",
                    }
                },
                auth_key=self.auth_key,
                session_key=self.session_key,
            )
            await ws.send_str(json.dumps(response, separators=(",", ":")))
