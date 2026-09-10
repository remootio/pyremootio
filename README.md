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

`connect()` runs HELLO, AUTH, and the first QUERY action to authenticate the client. After that, `listen()` receives device events.

Logging uses the standard `pyremootio` logger:

| Level | What you see |
| --- | --- |
| `DEBUG` | PING / PONG keepalive |
| `INFO` | connect, authenticate, disconnect (with reason) |
| `WARNING` | connection lost, reconnect failed |
| `ERROR` | unexpected receive-loop or listener failures |

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

`duration_minutes` holds the output active. It is rejected unless `api_version` is 3 or later. Failed actions raise `RemootioActionError`.

## Examples

- [`examples/trigger.py`](examples/trigger.py) — connect, trigger, wait for `StateChange`
- [`examples/log_events.py`](examples/log_events.py) — log device events to stdout

```bash
.venv/bin/python examples/trigger.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key>
```

## Tests

```bash
pytest
```
