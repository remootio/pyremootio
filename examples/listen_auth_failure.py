#!/usr/bin/env python3
"""Stay connected with reconnect=True and print AUTH failure notifications.

Invalid keys on the first AUTH raise from ``connect()``. ``listen_auth_failure``
fires if keys fail later; the client then stops.

Example (from this folder, after installing into .venv):

    .venv/bin/python examples/listen_auth_failure.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key>
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp

from pyremootio import RemootioAuthenticationError, RemootioClient


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--auth-key", required=True)
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("pyremootio").setLevel(logging.DEBUG)

    def on_auth_failure() -> None:
        logging.error("AUTH failed after the session was up; client stopped")

    def on_connection(connected: bool) -> None:
        if connected:
            logging.info("Authenticated")
        else:
            logging.warning("Disconnected")

    async with aiohttp.ClientSession() as session:
        client = RemootioClient(
            args.host,
            args.secret_key,
            args.auth_key,
            session,
            port=args.port,
        )
        client.listen_auth_failure(on_auth_failure)
        client.listen_connection(on_connection)
        try:
            await client.connect(reconnect=True)
        except RemootioAuthenticationError:
            logging.error("Invalid API keys")
            return
        logging.info(
            "Running with reconnect (serial=%s, authenticated=%s, is_running=%s)",
            client.serial_number,
            client.authenticated,
            client.is_running,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
