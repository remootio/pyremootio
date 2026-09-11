"""Async Remootio Websocket API client."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from inspect import isawaitable
from typing import Any, Self

import aiohttp

from pyremootio.const import (
    ACTION_ID_MASK,
    DEFAULT_ACTION_TIMEOUT,
    DEFAULT_AUTH_TIMEOUT,
    DEFAULT_FAIL_THRESHOLD,
    DEFAULT_HELLO_TIMEOUT,
    DEFAULT_PING_INTERVAL,
    DEFAULT_PORT,
    INITIAL_RECONNECT_DELAY,
    MAX_RECONNECT_DELAY,
    MAX_RECONNECT_DELAY_AUTH,
    MAX_RECONNECT_DELAY_CONN,
    MIN_API_VERSION_DURATION,
)
from pyremootio.crypto import compact_json, decrypt_frame, encrypt_payload
from pyremootio.exceptions import (
    RemootioActionError,
    RemootioAuthenticationError,
    RemootioConnectionError,
    RemootioCryptoError,
    RemootioError,
    RemootioTimeoutError,
)
from pyremootio.models import (
    ActionResponse,
    ActionType,
    Challenge,
    Credentials,
    DoorState,
    RemootioEvent,
    ServerHello,
)

_LOGGER = logging.getLogger(__name__)

EventCallback = Callable[[RemootioEvent], Awaitable[None] | None]
ConnectionCallback = Callable[[bool], Awaitable[None] | None]
FailureCallback = Callable[[], Awaitable[None] | None]


class RemootioClient:
    """Async client for one Remootio device."""

    def __init__(
        self,
        host: str,
        secret_key: str,
        auth_key: str,
        session: aiohttp.ClientSession,
        *,
        port: int = DEFAULT_PORT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        auth_timeout: float = DEFAULT_AUTH_TIMEOUT,
        auth_fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
    ) -> None:
        if ping_interval <= 0:
            raise ValueError("ping_interval must be positive")
        if auth_fail_threshold < 1:
            raise ValueError("auth_fail_threshold must be >= 1")
        self._host = host
        self._port = port
        self._credentials = Credentials(secret_key=secret_key, auth_key=auth_key)
        self._session = session
        self._ping_interval = ping_interval
        self._ping_timeout = ping_interval / 2
        self._action_timeout = action_timeout
        self._auth_timeout = auth_timeout
        self._auth_fail_threshold = auth_fail_threshold
        self._conn_fail_threshold = DEFAULT_FAIL_THRESHOLD
        self._auth_fail = 0
        self._conn_fail = 0
        self._auth_fail_notified = False
        self._conn_fail_notified = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY

        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session_key: str | None = None
        self._last_action_id: int | None = None
        self._authenticated = False
        self._state = DoorState.UNKNOWN
        self._server_hello: ServerHello | None = None

        self._receive_task: asyncio.Task[None] | None = None
        self._ping_task: asyncio.Task[None] | None = None
        self._ping_watchdog: asyncio.Task[None] | None = None
        self._reconnect_task: asyncio.Task[None] | None = None

        self._reconnect = False
        self._closed_by_user = False
        self._shutting_down = False
        self._action_lock = asyncio.Lock()
        self._pending: dict[int, asyncio.Future[ActionResponse]] = {}
        self._authenticated_event = asyncio.Event()
        self._hello_event = asyncio.Event()
        self._auth_error: BaseException | None = None
        self._disconnect_reason: str | None = None
        self._listeners: list[EventCallback] = []
        self._connection_listeners: list[ConnectionCallback] = []
        self._auth_failure_listeners: list[FailureCallback] = []
        self._connect_failure_listeners: list[FailureCallback] = []
        self._callback_tasks: set[asyncio.Task[None]] = set()

    @classmethod
    def from_credentials(
        cls,
        host: str,
        credentials: Credentials,
        session: aiohttp.ClientSession,
        *,
        port: int = DEFAULT_PORT,
        ping_interval: float = DEFAULT_PING_INTERVAL,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        auth_timeout: float = DEFAULT_AUTH_TIMEOUT,
        auth_fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
    ) -> RemootioClient:
        return cls(
            host,
            credentials.secret_key,
            credentials.auth_key,
            session,
            port=port,
            ping_interval=ping_interval,
            action_timeout=action_timeout,
            auth_timeout=auth_timeout,
            auth_fail_threshold=auth_fail_threshold,
        )

    @property
    def host(self) -> str:
        return self._host

    @property
    def port(self) -> int:
        return self._port

    @property
    def credentials(self) -> Credentials:
        return self._credentials

    @property
    def state(self) -> DoorState:
        return self._state

    @property
    def serial_number(self) -> str | None:
        if self._server_hello is None:
            return None
        return self._server_hello.serial_number

    @property
    def api_version(self) -> int | None:
        if self._server_hello is None:
            return None
        return self._server_hello.api_version

    @property
    def remootio_version(self) -> str | None:
        if self._server_hello is None:
            return None
        return self._server_hello.remootio_version

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    @property
    def authenticated(self) -> bool:
        return self.connected and self._authenticated and self._session_key is not None

    def __repr__(self) -> str:
        return (
            f"RemootioClient(host={self._host!r}, port={self._port}, "
            f"authenticated={self.authenticated})"
        )

    def listen(self, callback: EventCallback) -> Callable[[], None]:
        """Subscribe to decrypted device events. Returns an unsubscribe callback."""
        self._listeners.append(callback)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._listeners.remove(callback)

        return _unsubscribe

    def listen_connection(self, callback: ConnectionCallback) -> Callable[[], None]:
        """Subscribe to connection availability changes (True = authenticated)."""
        self._connection_listeners.append(callback)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._connection_listeners.remove(callback)

        return _unsubscribe

    def listen_auth_failure(self, callback: FailureCallback) -> Callable[[], None]:
        """Subscribe when consecutive AUTH failures hit ``auth_fail_threshold``."""
        self._auth_failure_listeners.append(callback)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._auth_failure_listeners.remove(callback)

        return _unsubscribe

    def listen_connect_failure(self, callback: FailureCallback) -> Callable[[], None]:
        """Subscribe when consecutive TCP connect failures hit the connect threshold."""
        self._connect_failure_listeners.append(callback)

        def _unsubscribe() -> None:
            with suppress(ValueError):
                self._connect_failure_listeners.remove(callback)

        return _unsubscribe

    async def connect(self, *, reconnect: bool = False) -> None:
        """Open the websocket, authenticate, and optionally keep reconnecting.

        With ``reconnect=False`` (the default), the first failure is raised.
        With ``reconnect=True``, the first failure is counted and retries continue
        until ``disconnect()``.
        """
        self._reconnect = reconnect
        self._closed_by_user = False
        try:
            await self._establish()
        except (
            RemootioConnectionError,
            RemootioAuthenticationError,
            RemootioCryptoError,
        ) as err:
            if not reconnect:
                raise
            self._record_failure(err)
        if reconnect and (self._reconnect_task is None or self._reconnect_task.done()):
            self._reconnect_task = asyncio.create_task(
                self._reconnect_loop(),
                name="pyremootio-reconnect",
            )

    async def disconnect(self) -> None:
        """Close the session and disable automatic reconnect."""
        self._closed_by_user = True
        self._reconnect = False
        self._set_disconnect_reason("client requested disconnect")
        if self._reconnect_task is not None:
            self._reconnect_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._reconnect_task
            self._reconnect_task = None
        await self._shutdown()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.disconnect()

    async def query(self) -> ActionResponse:
        return await self._request_action(ActionType.QUERY)

    async def trigger(self, duration_minutes: int | None = None) -> ActionResponse:
        return await self._request_action(ActionType.TRIGGER, duration_minutes)

    async def trigger_secondary(self, duration_minutes: int | None = None) -> ActionResponse:
        return await self._request_action(ActionType.TRIGGER_SECONDARY, duration_minutes)

    async def open(self, duration_minutes: int | None = None) -> ActionResponse:
        return await self._request_action(ActionType.OPEN, duration_minutes)

    async def close(self, duration_minutes: int | None = None) -> ActionResponse:
        return await self._request_action(ActionType.CLOSE, duration_minutes)

    async def restart(self) -> ActionResponse:
        return await self._request_action(ActionType.RESTART)

    async def _establish(self) -> None:
        if self.connected:
            self._set_disconnect_reason("replaced by new connection")
            await self._shutdown(notify=False)

        self._reset_session()
        url = f"ws://{self._host}:{self._port}/"
        _LOGGER.debug("Connecting to %s", url)
        try:
            self._ws = await self._session.ws_connect(
                url,
                heartbeat=None,
                autoping=False,
                compress=0,
            )
        except (aiohttp.ClientError, OSError) as err:
            raise RemootioConnectionError(f"Could not connect to {url}") from err

        self._conn_fail = 0
        self._conn_fail_notified = False
        _LOGGER.info("Connected to Remootio websocket (%s:%s)", self._host, self._port)

        self._receive_task = asyncio.create_task(
            self._receive_loop(),
            name="pyremootio-receive",
        )
        self._ping_task = asyncio.create_task(
            self._ping_loop(),
            name="pyremootio-ping",
        )

        await self._send_basic({"type": "HELLO"})
        # The device parses only one websocket frame per recv(). Sending AUTH in
        # the same TCP packet as HELLO would drop AUTH and stall authentication.
        with suppress(TimeoutError):
            await asyncio.wait_for(self._hello_event.wait(), timeout=DEFAULT_HELLO_TIMEOUT)
        await self._send_basic({"type": "AUTH"})

        try:
            await asyncio.wait_for(self._authenticated_event.wait(), timeout=self._auth_timeout)
        except TimeoutError as err:
            self._set_disconnect_reason("authentication timed out")
            await self._shutdown(notify=False)
            raise RemootioAuthenticationError("Authentication timed out") from err

        if self._auth_error is not None:
            error = self._auth_error
            self._set_disconnect_reason(str(error))
            await self._shutdown(notify=False)
            raise error

        if not self._authenticated:
            self._set_disconnect_reason("authentication failed")
            await self._shutdown(notify=False)
            raise RemootioAuthenticationError("Authentication failed")

        _LOGGER.info(
            "Authenticated with Remootio (%s:%s serial=%s api=%s state=%s)",
            self._host,
            self._port,
            self.serial_number,
            self.api_version,
            self._state,
        )
        self._auth_fail = 0
        self._auth_fail_notified = False
        self._reconnect_delay = INITIAL_RECONNECT_DELAY
        self._notify_connection(True)

    async def _reconnect_loop(self) -> None:
        while self._reconnect and not self._closed_by_user:
            if self.authenticated:
                if self._receive_task is None:
                    return
                with suppress(asyncio.CancelledError):
                    await self._receive_task
            if self._closed_by_user or not self._reconnect:
                return
            _LOGGER.warning(
                "Remootio connection lost (%s:%s): %s; reconnecting in %.1fs",
                self._host,
                self._port,
                self._disconnect_reason or "unknown",
                self._reconnect_delay,
            )
            await asyncio.sleep(self._reconnect_delay)
            if self._closed_by_user or not self._reconnect:
                return
            try:
                await self._establish()
            except (
                RemootioConnectionError,
                RemootioAuthenticationError,
                RemootioCryptoError,
            ) as err:
                self._record_failure(err)
            except RemootioError as err:
                _LOGGER.warning("Reconnect failed (%s:%s): %s", self._host, self._port, err)
                self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)
            except Exception:
                _LOGGER.exception(
                    "Unexpected error while reconnecting to Remootio (%s:%s)",
                    self._host,
                    self._port,
                )
                self._reconnect_delay = min(self._reconnect_delay * 2, MAX_RECONNECT_DELAY)

    def _record_failure(self, err: BaseException) -> None:
        """Count a failed attempt and raise the backoff cap after the threshold."""
        if isinstance(err, RemootioConnectionError):
            self._conn_fail += 1
            _LOGGER.warning(
                "Remootio connect failed (%s:%s): %s (%s/%s)",
                self._host,
                self._port,
                err,
                self._conn_fail,
                self._conn_fail_threshold,
            )
            if self._conn_fail >= self._conn_fail_threshold and not self._conn_fail_notified:
                self._conn_fail_notified = True
                _LOGGER.error(
                    "Remootio TCP connect failed %s times (%s:%s)",
                    self._conn_fail,
                    self._host,
                    self._port,
                )
                self._notify_connect_failure()
            cap = (
                MAX_RECONNECT_DELAY_CONN
                if self._conn_fail >= self._conn_fail_threshold
                else MAX_RECONNECT_DELAY
            )
        else:
            self._auth_fail += 1
            _LOGGER.warning(
                "Remootio authentication did not complete (%s:%s): %s (%s/%s)",
                self._host,
                self._port,
                err,
                self._auth_fail,
                self._auth_fail_threshold,
            )
            if self._auth_fail >= self._auth_fail_threshold and not self._auth_fail_notified:
                self._auth_fail_notified = True
                _LOGGER.error(
                    "Remootio AUTH failed %s times (%s:%s)",
                    self._auth_fail,
                    self._host,
                    self._port,
                )
                self._notify_auth_failure()
            cap = (
                MAX_RECONNECT_DELAY_AUTH
                if self._auth_fail >= self._auth_fail_threshold
                else MAX_RECONNECT_DELAY
            )
        self._reconnect_delay = min(self._reconnect_delay * 2, cap)

    def _reset_session(self) -> None:
        self._session_key = None
        self._last_action_id = None
        self._authenticated = False
        self._auth_error = None
        self._shutting_down = False
        self._authenticated_event = asyncio.Event()
        self._hello_event = asyncio.Event()
        self._disconnect_reason = None
        self._fail_pending(RemootioConnectionError("Connection was reset"))

    async def _shutdown(self, *, notify: bool = True, from_receive: bool = False) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        was_authenticated = self._authenticated
        had_connection = self._ws is not None or was_authenticated
        self._authenticated = False
        self._session_key = None
        self._fail_pending(RemootioConnectionError("Disconnected from Remootio"))

        tasks = [self._ping_watchdog, self._ping_task, *self._callback_tasks]
        if not from_receive:
            tasks.append(self._receive_task)
        for task in tasks:
            if task is not None and not task.done():
                task.cancel()
        for task in tasks:
            if task is not None:
                with suppress(asyncio.CancelledError):
                    await task
        self._callback_tasks.clear()
        self._ping_watchdog = None
        self._ping_task = None
        if not from_receive:
            self._receive_task = None

        if self._ws is not None and not self._ws.closed:
            with suppress(aiohttp.ClientError):
                await self._ws.close()
        self._ws = None
        reason = self._disconnect_reason or "unknown"
        if had_connection:
            _LOGGER.info(
                "Disconnected from Remootio (%s:%s): %s",
                self._host,
                self._port,
                reason,
            )
        self._shutting_down = False

        if notify and was_authenticated:
            self._notify_connection(False)

    async def _receive_loop(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for message in ws:
                self._clear_ping_watchdog()
                if message.type in {aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY}:
                    raw = message.data
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8", errors="replace")
                    await self._handle_message(raw)
                elif message.type == aiohttp.WSMsgType.ERROR:
                    exception = ws.exception()
                    self._set_disconnect_reason(f"websocket error: {exception or 'unknown'}")
                    break
                elif message.type in {
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                }:
                    close_code = ws.close_code
                    if close_code is not None:
                        self._set_disconnect_reason(f"websocket closed (code={close_code})")
                    else:
                        self._set_disconnect_reason("websocket closed")
                    break
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self._set_disconnect_reason(f"receive loop failed: {err}")
            _LOGGER.exception("Remootio receive loop failed")
        finally:
            if self._disconnect_reason is None:
                self._set_disconnect_reason("websocket closed")
            if not self._closed_by_user:
                await self._shutdown(from_receive=True)

    async def _handle_message(self, raw: str) -> None:
        try:
            frame = json.loads(raw)
        except json.JSONDecodeError:
            _LOGGER.debug("Ignoring non-JSON frame")
            return
        if not isinstance(frame, dict):
            return

        frame_type = frame.get("type")
        if frame_type == "SERVER_HELLO":
            self._server_hello = ServerHello.from_frame(frame)
            self._hello_event.set()
            return
        if frame_type == "PONG":
            _LOGGER.debug("Received PONG from Remootio (%s:%s)", self._host, self._port)
            return
        if frame_type == "ERROR":
            self._handle_error_frame(str(frame.get("errorMessage", "")))
            return
        if frame_type != "ENCRYPTED":
            _LOGGER.debug("Ignoring frame type %s", frame_type)
            return

        try:
            payload = decrypt_frame(
                frame,
                auth_key=self._credentials.auth_key,
                secret_key=self._credentials.secret_key,
                session_key=self._session_key,
            )
        except RemootioCryptoError as err:
            _LOGGER.debug("Failed to decrypt frame: %s", err)
            if not self._authenticated:
                self._fail_authentication(RemootioAuthenticationError("Invalid API keys"))
            return

        if "challenge" in payload:
            await self._handle_challenge(payload)
            return
        if "response" in payload:
            self._handle_response(payload)
            return
        if "event" in payload:
            self._handle_event(payload)

    def _handle_error_frame(self, error_message: str) -> None:
        _LOGGER.debug("Device error: %s", error_message)
        self._set_disconnect_reason(f"device error: {error_message}")
        if error_message in {
            "authentication error",
            "authentication timeout",
            "already authenticated",
        }:
            self._fail_authentication(RemootioAuthenticationError(error_message))

    async def _handle_challenge(self, payload: dict[str, Any]) -> None:
        try:
            challenge = Challenge.from_payload(payload)
        except ValueError as err:
            self._fail_authentication(RemootioAuthenticationError(str(err)))
            return
        self._session_key = challenge.session_key
        self._last_action_id = challenge.initial_action_id
        try:
            await self._send_action(ActionType.QUERY, wait=False)
        except RemootioError as err:
            self._fail_authentication(err)

    def _handle_response(self, payload: dict[str, Any]) -> None:
        try:
            response = ActionResponse.from_payload(payload)
        except ValueError:
            _LOGGER.debug("Ignoring malformed action response")
            return
        self._note_response_id(response.id)
        self._state = response.state
        if (
            not self._authenticated
            and response.type is ActionType.QUERY
            and self._session_key is not None
        ):
            self._authenticated = True
            self._authenticated_event.set()
        pending = self._pending.pop(response.id, None)
        if pending is not None and not pending.done():
            pending.set_result(response)

    def _handle_event(self, payload: dict[str, Any]) -> None:
        try:
            event = RemootioEvent.from_payload(payload)
        except ValueError:
            _LOGGER.debug("Ignoring malformed event")
            return
        if event.state is not DoorState.UNKNOWN:
            self._state = event.state
        self._notify_event(event)

    def _fail_authentication(self, error: BaseException) -> None:
        self._auth_error = error
        self._authenticated_event.set()

    def _note_response_id(self, response_id: int) -> None:
        if self._last_action_id is None:
            self._last_action_id = response_id
            return
        if response_id > self._last_action_id or (
            response_id == 0 and self._last_action_id == ACTION_ID_MASK - 1
        ):
            self._last_action_id = response_id

    def _allocate_action_id(self) -> int:
        if self._last_action_id is None:
            raise RemootioAuthenticationError("Session is not authenticated")
        self._last_action_id = (self._last_action_id + 1) % ACTION_ID_MASK
        return self._last_action_id

    async def _request_action(
        self,
        action_type: ActionType,
        duration_minutes: int | None = None,
    ) -> ActionResponse:
        if not self.authenticated:
            raise RemootioAuthenticationError("Session is not authenticated")
        if duration_minutes is not None:
            if duration_minutes < 1:
                raise ValueError("duration_minutes must be >= 1")
            version = self.api_version or 0
            if version < MIN_API_VERSION_DURATION:
                raise ValueError(
                    "duration_minutes requires Websocket API v3 or later "
                    f"(device reports v{version or 'unknown'})"
                )
        response = await self._send_action(action_type, duration_minutes=duration_minutes)
        if not response.success:
            raise RemootioActionError(response)
        return response

    async def _send_action(
        self,
        action_type: ActionType,
        duration_minutes: int | None = None,
        *,
        wait: bool = True,
    ) -> ActionResponse:
        async with self._action_lock:
            action_id = self._allocate_action_id()
            action: dict[str, Any] = {"type": action_type.value, "id": action_id}
            if duration_minutes is not None:
                action["duration"] = duration_minutes
            loop = asyncio.get_running_loop()
            future: asyncio.Future[ActionResponse] = loop.create_future()
            self._pending[action_id] = future
            await self._send_encrypted({"action": action})

        if not wait:
            return ActionResponse(
                type=action_type,
                id=action_id,
                success=True,
                state=self._state,
                t100ms=0,
                relay_triggered=False,
                error_code="",
            )

        try:
            return await asyncio.wait_for(future, timeout=self._action_timeout)
        except TimeoutError as err:
            self._pending.pop(action_id, None)
            if not future.done():
                future.cancel()
            raise RemootioTimeoutError(
                f"Timed out waiting for {action_type.value} response"
            ) from err

    async def _send_encrypted(self, payload: dict[str, Any]) -> None:
        if self._session_key is None:
            raise RemootioAuthenticationError("Session is not authenticated")
        frame = encrypt_payload(
            payload,
            auth_key=self._credentials.auth_key,
            session_key=self._session_key,
        )
        await self._send_raw(frame)

    async def _send_basic(self, frame: dict[str, Any]) -> None:
        await self._send_raw(frame)

    async def _send_raw(self, frame: dict[str, Any]) -> None:
        if self._ws is None or self._ws.closed:
            raise RemootioConnectionError("Not connected to Remootio")
        await self._ws.send_str(compact_json(frame))

    async def _ping_loop(self) -> None:
        try:
            while self.connected and not self._closed_by_user:
                await asyncio.sleep(self._ping_interval)
                if not self.connected or self._closed_by_user:
                    return
                self._arm_ping_watchdog()
                with suppress(RemootioConnectionError):
                    await self._send_basic({"type": "PING"})
                    _LOGGER.debug("Sent PING to Remootio (%s:%s)", self._host, self._port)
        except asyncio.CancelledError:
            raise

    def _arm_ping_watchdog(self) -> None:
        self._clear_ping_watchdog()
        self._ping_watchdog = asyncio.create_task(
            self._ping_watchdog_timeout(),
            name="pyremootio-ping-watchdog",
        )

    def _clear_ping_watchdog(self) -> None:
        if self._ping_watchdog is not None:
            self._ping_watchdog.cancel()
            self._ping_watchdog = None

    async def _ping_watchdog_timeout(self) -> None:
        try:
            await asyncio.sleep(self._ping_timeout)
        except asyncio.CancelledError:
            return
        self._set_disconnect_reason(
            f"no response to PING within {int(self._ping_timeout * 1000)} ms"
        )
        if self._ws is not None and not self._ws.closed:
            await self._ws.close()

    def _set_disconnect_reason(self, reason: str) -> None:
        if self._disconnect_reason is None:
            self._disconnect_reason = reason

    def _fail_pending(self, error: BaseException) -> None:
        message = str(error)
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RemootioConnectionError(message))
        self._pending.clear()

    def _notify_event(self, event: RemootioEvent) -> None:
        for callback in list(self._listeners):
            self._schedule_callback(callback, event)

    def _notify_connection(self, available: bool) -> None:
        for callback in list(self._connection_listeners):
            self._schedule_callback(callback, available)

    def _notify_auth_failure(self) -> None:
        for callback in list(self._auth_failure_listeners):
            self._schedule_callback(callback)

    def _notify_connect_failure(self) -> None:
        for callback in list(self._connect_failure_listeners):
            self._schedule_callback(callback)

    def _schedule_callback(
        self,
        callback: Callable[..., Awaitable[None] | None],
        *args: Any,
    ) -> None:
        task = asyncio.create_task(self._run_callback(callback, *args))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)

    async def _run_callback(
        self,
        callback: Callable[..., Awaitable[None] | None],
        *args: Any,
    ) -> None:
        try:
            result = callback(*args)
            if isawaitable(result):
                await result
        except Exception:
            _LOGGER.exception("Remootio listener raised")
