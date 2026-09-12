# pyremootio

Async Python client for the [Remootio](https://www.remootio.com) local Websocket API.

Enable the Websocket API in the Remootio app and copy the API Secret Key, API Auth Key, and device IP.

Protocol details: [Remootio Websocket API documentation](https://github.com/remootio/remootio-api-documentation).

## Install

Python 3.12 or newer:

```bash
pip install pyremootio
```

From a clone of this repo (includes tests and examples):

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

Run examples with `.venv/bin/python`.

## Usage

```python
import asyncio
import aiohttp
from pyremootio import RemootioClient

async def main() -> None:
    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            "<ip_address>",  # IP address of your Remootio device
            "<secret_key>",  # API Secret Key from the Remootio app
            "<auth_key>",  # API Auth Key from the Remootio app
            session,
        )
        await client.connect()
        try:
            response = await client.trigger()
            print("TRIGGER success=%s state=%s" % (response.success, response.state))
        finally:
            await client.disconnect()

asyncio.run(main())
```

`connect()` opens the websocket and authenticates. Invalid API keys raise `RemootioAuthenticationError`. A missing `SERVER_HELLO` or a TCP failure raises as well. After a successful `connect()`, call `enable_reconnect()` to keep the session alive without opening a second websocket.

`connect(reconnect=True)` retries TCP failures and handshake timeouts until `disconnect()`. Invalid keys raise `RemootioAuthenticationError` from the `connect()` call. If AUTH fails later — for example the keys were changed on the device — `listen_auth_failure` runs once, reconnect stops, and `is_running` becomes False. `is_running` is True while a session is up or TCP/timeout retries are in progress, and False after `disconnect()` or that AUTH stop. 

What happens in the background:

1. Open the websocket. TCP / upgrade failure → `RemootioConnectionError`.
2. Send `HELLO` and wait for `SERVER_HELLO`. No reply → `RemootioTimeoutError`.
3. Send `AUTH`. The device replies with an encrypted challenge.
4. Verify the challenge with the secret keys. Failure → `RemootioAuthenticationError("Invalid API keys")`.
5. Send `QUERY` to finish AUTH. A device `authentication error` frame → `RemootioAuthenticationError`.
6. If `HELLO` succeeded but AUTH never completes (no challenge, or no `QUERY` response) → `RemootioTimeoutError`.
7. A `QUERY` response → the session is authenticated.

Subscribe to `listen_auth_failure` before `connect()` if you need that later-AUTH callback:

```python
from pyremootio import RemootioAuthenticationError

def on_auth_failure():
    print("API keys are no longer valid; client stopped")

unsubscribe = client.listen_auth_failure(on_auth_failure)
try:
    await client.connect(reconnect=True)
except RemootioAuthenticationError:
    print("Invalid API keys")
```

Logging uses the standard `pyremootio` logger:

| Level | What you see |
| --- | --- |
| `DEBUG` | PING / PONG, connect/disconnect, reconnect retries, per-attempt TCP failures |
| `INFO` | authenticated session |
| `ERROR` | AUTH failed (client stopped); TCP connect hit the failure threshold; unexpected receive-loop or listener failures |

```python
import logging
logging.getLogger("pyremootio").setLevel(logging.DEBUG)
```

### Events

```python
async def on_event(event):
    print(event.type, event.state)

unsubscribe = client.listen(on_event)
```

`StateChange` is sent when a sensor is installed. Enable API logging in the app for the full event set.

### Actions

| Method | Device action |
| --- | --- |
| `query()` | Current door state |
| `open(duration_minutes=None)` | Open if closed (sensor required) |
| `close(duration_minutes=None)` | Close if open (sensor required) |
| `trigger(duration_minutes=None)` | Pulse the control output |
| `trigger_secondary(duration_minutes=None)` | Pulse the free relay output |
| `restart()` | Reboot the device (connection drops) |

`duration_minutes` holds the output active. It is rejected unless `api_version` is 3 or later. 

Failed actions raise `RemootioActionError`. A timed-out action closes the websocket so a client started with `connect(reconnect=True)` can AUTH again. AUTH is not sent until `SERVER_HELLO` arrives. A missing challenge or `QUERY` response is a timeout, not invalid keys.

## Examples

- [`examples/trigger.py`](examples/trigger.py) — connect, trigger, wait for `StateChange`
- [`examples/log_events.py`](examples/log_events.py) — log device events to stdout
- [`examples/listen_auth_failure.py`](examples/listen_auth_failure.py) — `connect(reconnect=True)` and print when AUTH fails after the session was up

```bash
.venv/bin/python examples/trigger.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key>
```

## Tests

```bash
pytest
```

## Acknowledgments

Special thanks to [Gergő Gábor Ilyes-Veisz](https://github.com/ivgg-me) for [aioremootio](https://github.com/ivgg-me/aioremootio), for creating his python Remootio API client while no official package was available.
