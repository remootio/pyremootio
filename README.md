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

`connect()` is one-shot. If it cannot connect or complete AUTH, it raises immediately. After a successful `connect()`, call `enable_reconnect()` to keep the session alive without opening a second websocket.

`connect(reconnect=True)` is for a long-lived client. It does not raise on connect/AUTH failure; it keeps retrying until `disconnect()`. After consecutive AUTH failures hit `auth_fail_threshold`, `listen_auth_failure` subscribers are notified. Reconnect does not stop.

Subscribe **before** `connect(reconnect=True)`:

```python
def on_auth_failure():
    print("AUTH failed repeatedly; API keys may have changed")

unsubscribe = client.listen_auth_failure(on_auth_failure)
await client.connect(reconnect=True)
```

Logging uses the standard `pyremootio` logger:

| Level | What you see |
| --- | --- |
| `DEBUG` | PING / PONG, connect/disconnect, reconnect retries, per-attempt AUTH / TCP failures |
| `INFO` | authenticated session |
| `ERROR` | AUTH or TCP connect hit the failure threshold; unexpected receive-loop or listener failures |

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

Failed actions raise `RemootioActionError`. A timed-out action closes the websocket so a client started with `connect(reconnect=True)` can AUTH again. AUTH is not sent until `SERVER_HELLO` arrives, so `listen_auth_failure` only counts failures after the device has already replied, so the api client does not assume credentials are wrong for device that is only  busy temporarily.

## Examples

- [`examples/trigger.py`](examples/trigger.py) — connect, trigger, wait for `StateChange`
- [`examples/log_events.py`](examples/log_events.py) — log device events to stdout
- [`examples/listen_auth_failure.py`](examples/listen_auth_failure.py) — `connect(reconnect=True)` and print when AUTH failures hit the threshold

```bash
.venv/bin/python examples/trigger.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key>
```

## Tests

```bash
pytest
```

## Acknowledgments

Special thanks to [Gergő Gábor Ilyes-Veisz](https://github.com/ivgg-me) for [aioremootio](https://github.com/ivgg-me/aioremootio), for creating his python Remootio API client while no official package was available.
