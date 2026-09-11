"""End-to-end client tests against an in-process fake Remootio device."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from pyremootio import (
    Credentials,
    DoorState,
    EventType,
    RemootioActionError,
    RemootioAuthenticationError,
    RemootioClient,
    RemootioConnectionError,
    RemootioEvent,
    RemootioTimeoutError,
)
from tests.fake_device import (
    SPEC_AUTH_KEY,
    SPEC_SECRET_KEY,
    FakeRemootioDevice,
)


@pytest.fixture
async def fake_device() -> FakeRemootioDevice:
    return FakeRemootioDevice()


@pytest.fixture
async def running_device(
    fake_device: FakeRemootioDevice,
) -> tuple[FakeRemootioDevice, str, int]:
    app = web.Application()
    app.router.add_get("/", fake_device.websocket_handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        assert server.port is not None
        yield fake_device, str(server.host), server.port
    finally:
        await server.close()


@asynccontextmanager
async def _serve(fake: FakeRemootioDevice) -> AsyncIterator[tuple[str, int]]:
    app = web.Application()
    app.router.add_get("/", fake.websocket_handler)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        assert server.port is not None
        yield str(server.host), server.port
    finally:
        await server.close()


class _SilentAfterAuthDevice(FakeRemootioDevice):
    async def _handle_frame(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        if frame.get("type") == "ENCRYPTED" and self._authenticated:
            return
        await super()._handle_frame(ws, frame)


class _SilentChallengeResponseDevice(FakeRemootioDevice):
    """Sends the AUTH challenge, then ignores the QUERY that would finish AUTH."""

    drop_encrypted = True

    async def _handle_frame(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        if self.drop_encrypted and frame.get("type") == "ENCRYPTED":
            return
        await super()._handle_frame(ws, frame)


class _NoHelloDevice(FakeRemootioDevice):
    """Accepts the websocket but never replies to HELLO."""

    async def _handle_frame(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
        if frame.get("type") == "HELLO":
            return
        await super()._handle_frame(ws, frame)


async def test_connect_authenticates_and_reports_identity(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    fake, host, port = running_device
    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        )
        await client.connect()
        try:
            assert client.connected is True
            assert client.authenticated is True
            assert client.serial_number == fake.serial_number
            assert client.api_version == 3
            assert client.remootio_version == "remootio-3"
            assert client.state is DoorState.CLOSED
        finally:
            await client.disconnect()
        assert client.connected is False
        assert client.authenticated is False


async def test_open_close_and_query(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    _fake, host, port = running_device
    async with (
        aiohttp.ClientSession() as session,
        RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        ) as client,
    ):
        opened = await client.open()
        assert opened.success is True
        assert opened.state is DoorState.OPEN
        assert client.state is DoorState.OPEN
        closed = await client.close()
        assert closed.state is DoorState.CLOSED
        queried = await client.query()
        assert queried.type.value == "QUERY"
        triggered = await client.trigger(duration_minutes=5)
        assert triggered.success is True


async def test_duration_requires_api_v3() -> None:
    async with (
        _serve(FakeRemootioDevice(api_version=2)) as (host, port),
        aiohttp.ClientSession() as session,
        RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        ) as client,
    ):
        assert client.api_version == 2
        with pytest.raises(ValueError, match="API v3"):
            await client.trigger(duration_minutes=5)
        triggered = await client.trigger()
        assert triggered.success is True


async def test_state_change_listener(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    fake, host, port = running_device
    events: list[RemootioEvent] = []

    async def _on_event(event: RemootioEvent) -> None:
        events.append(event)

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        )
        unsub = client.listen(_on_event)
        await client.connect()
        try:
            await fake.push_event(
                {"cnt": 72, "type": "StateChange", "state": "open", "t100ms": 18342}
            )
            await asyncio.sleep(0.05)
            assert client.state is DoorState.OPEN
            assert len(events) == 1
            assert events[0].event_type is EventType.STATE_CHANGE
        finally:
            unsub()
            await client.disconnect()


async def test_wrong_keys_fail_authentication(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    _fake, host, port = running_device
    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            "AA" * 32,
            "BB" * 32,
            session,
            port=port,
            ping_interval=10,
            auth_timeout=1,
        )
        with pytest.raises(RemootioAuthenticationError):
            await client.connect()


async def test_device_authentication_error() -> None:
    async with (
        _serve(FakeRemootioDevice(fail_auth=True)) as (host, port),
        aiohttp.ClientSession() as session,
    ):
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
            auth_timeout=1,
        )
        with pytest.raises(RemootioAuthenticationError, match="authentication error"):
            await client.connect()


async def test_action_error_raises() -> None:
    fake = FakeRemootioDevice(action_error="ERR_NO_SENSOR", door_state="no sensor")
    async with (
        _serve(fake) as (host, port),
        aiohttp.ClientSession() as session,
        RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        ) as client,
    ):
        # Auth QUERY also gets action_error; connect should still succeed
        # because QUERY during auth does not go through _request_action.
        assert client.authenticated is True
        with pytest.raises(RemootioActionError, match="ERR_NO_SENSOR"):
            await client.open()


async def test_action_timeout() -> None:
    async with (
        _serve(_SilentAfterAuthDevice()) as (host, port),
        aiohttp.ClientSession() as session,
        RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
            action_timeout=0.2,
        ) as client,
    ):
        with pytest.raises(RemootioTimeoutError):
            await client.trigger()
        assert client.authenticated is False


async def test_action_timeout_reconnects_and_auths() -> None:
    lost = asyncio.Event()
    restored = asyncio.Event()

    def on_connection(connected: bool) -> None:
        if connected:
            if lost.is_set():
                restored.set()
        else:
            lost.set()

    with patch("pyremootio.client.INITIAL_RECONNECT_DELAY", 0.05):
        async with (
            _serve(_SilentAfterAuthDevice()) as (host, port),
            aiohttp.ClientSession() as session,
        ):
            client = RemootioClient(
                host,
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=port,
                ping_interval=10,
                action_timeout=0.2,
            )
            client.listen_connection(on_connection)
            await client.connect(reconnect=True)
            try:
                with pytest.raises(RemootioTimeoutError):
                    await client.trigger()
                await asyncio.wait_for(lost.wait(), timeout=2)
                assert client.authenticated is False
                await asyncio.wait_for(restored.wait(), timeout=2)
                assert client.authenticated is True
            finally:
                await client.disconnect()


async def test_unreachable_host_raises_connection_error() -> None:
    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            "127.0.0.1",
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=1,
            ping_interval=10,
        )
        with pytest.raises(RemootioConnectionError):
            await client.connect()


async def test_connect_timeout_when_handshake_hangs() -> None:
    """TCP accepts but the websocket upgrade never completes."""

    async def hang(_request: web.Request) -> web.WebSocketResponse:
        await asyncio.Event().wait()
        raise AssertionError("handshake should have timed out")

    app = web.Application()
    app.router.add_get("/", hang)
    server = TestServer(app, host="127.0.0.1")
    await server.start_server()
    try:
        assert server.port is not None
        async with aiohttp.ClientSession() as session:
            client = RemootioClient(
                str(server.host),
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=server.port,
                ping_interval=10,
                connect_timeout=0.2,
            )
            with pytest.raises(RemootioConnectionError, match="Timed out connecting"):
                await client.connect()
    finally:
        await server.close()


async def test_from_credentials_and_repr(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    _fake, host, port = running_device
    credentials = Credentials(secret_key=SPEC_SECRET_KEY, auth_key=SPEC_AUTH_KEY)
    async with aiohttp.ClientSession() as session:
        client = RemootioClient.from_credentials(
            host,
            credentials,
            session,
            port=port,
            ping_interval=10,
        )
        assert SPEC_SECRET_KEY not in repr(client)
        assert SPEC_AUTH_KEY not in repr(client)
        await client.connect()
        try:
            assert client.authenticated is True
        finally:
            await client.disconnect()


async def test_concurrent_queries(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    _fake, host, port = running_device
    async with (
        aiohttp.ClientSession() as session,
        RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        ) as client,
    ):
        results = await asyncio.gather(client.query(), client.query(), client.query())
        assert all(result.success for result in results)


async def test_disconnect_fails_in_flight_actions() -> None:
    async with (
        _serve(_SilentAfterAuthDevice()) as (host, port),
        aiohttp.ClientSession() as session,
    ):
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
            action_timeout=5,
        )
        await client.connect()
        pending = [
            asyncio.create_task(client.trigger()),
            asyncio.create_task(client.query()),
        ]
        await asyncio.sleep(0.05)
        await client.disconnect()
        for task in pending:
            with pytest.raises(RemootioConnectionError):
                await task


async def test_reconnects_after_device_closes(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    fake, host, port = running_device
    unavailable = asyncio.Event()
    available = asyncio.Event()

    def on_connection(connected: bool) -> None:
        if connected:
            available.set()
        else:
            unavailable.set()

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        )
        client.listen_connection(on_connection)
        await client.connect(reconnect=True)
        try:
            await asyncio.wait_for(available.wait(), timeout=1)
            available.clear()
            assert fake._ws is not None
            await fake._ws.close()
            await asyncio.wait_for(unavailable.wait(), timeout=2)
            await asyncio.wait_for(available.wait(), timeout=5)
            assert client.authenticated is True
            queried = await client.query()
            assert queried.success is True
        finally:
            await client.disconnect()


async def test_enable_reconnect_after_one_shot_connect(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    fake, host, port = running_device
    unavailable = asyncio.Event()
    available = asyncio.Event()

    def on_connection(connected: bool) -> None:
        if connected:
            available.set()
        else:
            unavailable.set()

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
        )
        client.listen_connection(on_connection)
        await client.connect()
        client.enable_reconnect()
        try:
            await asyncio.wait_for(available.wait(), timeout=1)
            available.clear()
            assert fake._ws is not None
            await fake._ws.close()
            await asyncio.wait_for(unavailable.wait(), timeout=2)
            await asyncio.wait_for(available.wait(), timeout=5)
            assert client.authenticated is True
        finally:
            await client.disconnect()


async def test_ping_without_pong_disconnects() -> None:
    class NoPongDevice(FakeRemootioDevice):
        async def _handle_frame(self, ws: web.WebSocketResponse, frame: dict[str, Any]) -> None:
            if frame.get("type") == "PING":
                return
            await super()._handle_frame(ws, frame)

    lost = asyncio.Event()

    def on_connection(connected: bool) -> None:
        if not connected:
            lost.set()

    async with (
        _serve(NoPongDevice()) as (host, port),
        aiohttp.ClientSession() as session,
    ):
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=0.2,
        )
        client.listen_connection(on_connection)
        await client.connect()
        try:
            await asyncio.wait_for(lost.wait(), timeout=2)
            assert client.authenticated is False
        finally:
            await client.disconnect()


async def test_rejects_non_positive_ping_interval() -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="positive"):
            RemootioClient(
                "127.0.0.1",
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                ping_interval=0,
            )


async def test_rejects_non_positive_connect_timeout() -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="connect_timeout"):
            RemootioClient(
                "127.0.0.1",
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                connect_timeout=0,
            )


async def test_reconnect_auth_failures_from_first_attempt_then_recovers() -> None:
    fake = _SilentChallengeResponseDevice()
    auth_failed = asyncio.Event()
    authenticated = asyncio.Event()

    def on_auth_failure() -> None:
        auth_failed.set()

    def on_connection(connected: bool) -> None:
        if connected:
            authenticated.set()

    with (
        patch("pyremootio.client.INITIAL_RECONNECT_DELAY", 0.05),
        patch("pyremootio.client.MAX_RECONNECT_DELAY", 0.1),
        patch("pyremootio.client.MAX_RECONNECT_DELAY_AUTH", 0.2),
    ):
        async with (
            _serve(fake) as (host, port),
            aiohttp.ClientSession() as session,
        ):
            client = RemootioClient(
                host,
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=port,
                ping_interval=10,
                auth_timeout=0.3,
                auth_fail_threshold=2,
            )
            client.listen_auth_failure(on_auth_failure)
            client.listen_connection(on_connection)
            await client.connect(reconnect=True)
            try:
                await asyncio.wait_for(auth_failed.wait(), timeout=5)
                assert client.authenticated is False
                fake.drop_encrypted = False
                await asyncio.wait_for(authenticated.wait(), timeout=5)
                assert client.authenticated is True
            finally:
                await client.disconnect()


async def test_reconnect_connect_failures_do_not_count_as_auth() -> None:
    connect_failed = asyncio.Event()
    auth_failed = asyncio.Event()

    def on_connect_failure() -> None:
        connect_failed.set()

    def on_auth_failure() -> None:
        auth_failed.set()

    with (
        patch("pyremootio.client.INITIAL_RECONNECT_DELAY", 0.05),
        patch("pyremootio.client.MAX_RECONNECT_DELAY", 0.1),
        patch("pyremootio.client.MAX_RECONNECT_DELAY_CONN", 0.15),
    ):
        async with aiohttp.ClientSession() as session:
            client = RemootioClient(
                "127.0.0.1",
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=1,
                ping_interval=10,
            )
            client._conn_fail_threshold = 2
            client.listen_connect_failure(on_connect_failure)
            client.listen_auth_failure(on_auth_failure)
            await client.connect(reconnect=True)
            try:
                await asyncio.wait_for(connect_failed.wait(), timeout=5)
                assert not auth_failed.is_set()
                assert client.authenticated is False
            finally:
                await client.disconnect()


async def test_reconnects_after_device_closes_does_not_fire_auth_failure(
    running_device: tuple[FakeRemootioDevice, str, int],
) -> None:
    fake, host, port = running_device
    auth_failed = asyncio.Event()

    def on_auth_failure() -> None:
        auth_failed.set()

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            host,
            SPEC_SECRET_KEY,
            SPEC_AUTH_KEY,
            session,
            port=port,
            ping_interval=10,
            auth_fail_threshold=2,
        )
        client.listen_auth_failure(on_auth_failure)
        await client.connect(reconnect=True)
        try:
            assert fake._ws is not None
            await fake._ws.close()
            await asyncio.sleep(1.5)
            assert client.authenticated is True
            assert not auth_failed.is_set()
        finally:
            await client.disconnect()


async def test_no_hello_does_not_count_as_auth_failure() -> None:
    auth_failed = asyncio.Event()

    def on_auth_failure() -> None:
        auth_failed.set()

    with (
        patch("pyremootio.client.DEFAULT_HELLO_TIMEOUT", 0.1),
        patch("pyremootio.client.INITIAL_RECONNECT_DELAY", 0.05),
        patch("pyremootio.client.MAX_RECONNECT_DELAY", 0.1),
    ):
        async with (
            _serve(_NoHelloDevice()) as (host, port),
            aiohttp.ClientSession() as session,
        ):
            client = RemootioClient(
                host,
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=port,
                ping_interval=10,
                auth_fail_threshold=2,
            )
            client.listen_auth_failure(on_auth_failure)
            await client.connect(reconnect=True)
            try:
                await asyncio.sleep(0.8)
                assert not auth_failed.is_set()
                assert client.authenticated is False
            finally:
                await client.disconnect()


async def test_connect_without_hello_raises_timeout() -> None:
    with patch("pyremootio.client.DEFAULT_HELLO_TIMEOUT", 0.1):
        async with (
            _serve(_NoHelloDevice()) as (host, port),
            aiohttp.ClientSession() as session,
        ):
            client = RemootioClient(
                host,
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                port=port,
                ping_interval=10,
            )
            with pytest.raises(RemootioTimeoutError, match="SERVER_HELLO"):
                await client.connect()


async def test_rejects_non_positive_auth_fail_threshold() -> None:
    async with aiohttp.ClientSession() as session:
        with pytest.raises(ValueError, match="auth_fail_threshold"):
            RemootioClient(
                "127.0.0.1",
                SPEC_SECRET_KEY,
                SPEC_AUTH_KEY,
                session,
                auth_fail_threshold=0,
            )
