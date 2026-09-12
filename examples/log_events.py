#!/usr/bin/env python3
"""Connect and print every event the device sends (enable API with logging).

Example (from this folder, after installing into .venv):

    <ip_address> is the IP address of the Remootio device.
    <secret_key> is the secret key of the Remootio device, you can find it in the Remootio app.
    <auth_key> is the auth key of the Remootio device, you can find it in the Remootio app.

    .venv/bin/python examples/log_events.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key>

    for example:
    .venv/bin/python examples/log_events.py --host 10.23.1.106 --secret-key D0A7E9B4E31AB7BCF2219C9A587ACA7DDB572EAB08459D0ACC52F7C83121B19C --auth-key 3C3B25A1227B9FA87E4A4E259B661F98E522531D2250BAE0C3F1FE3B02762183
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp

from pyremootio import RemootioAuthenticationError, RemootioClient, RemootioEvent


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--auth-key", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--log-level",
        default="DEBUG",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="pyremootio log level (default: DEBUG, includes PING/PONG)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("pyremootio").setLevel(getattr(logging, args.log_level))

    async def on_event(event: RemootioEvent) -> None:
        logging.info("%s state=%s cnt=%s data=%s", event.type, event.state, event.cnt, event.data)

    stopped = asyncio.Event()

    def on_auth_failure() -> None:
        logging.error("API keys are no longer valid; stopping")
        stopped.set()

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            args.host,
            args.secret_key,
            args.auth_key,
            session,
            port=args.port,
        )
        client.listen(on_event)
        client.listen_auth_failure(on_auth_failure)
        try:
            await client.connect(reconnect=True)
        except RemootioAuthenticationError:
            logging.error("Invalid API keys")
            return
        logging.info(
            "Listening (serial=%s, api=%s, state=%s)",
            client.serial_number,
            client.api_version,
            client.state,
        )
        try:
            await stopped.wait()
        finally:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
