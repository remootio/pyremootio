#!/usr/bin/env python3
"""Stay connected with reconnect=True and print AUTH failure notifications.

``listen_auth_failure`` fires once consecutive AUTH attempts (challenge then QUERY)
hit ``auth_fail_threshold`` (default 10). Reconnect keeps running.

Example (from this folder, after installing into .venv):

    .venv/bin/python examples/listen_auth_failure.py --host <ip_address> --secret-key <secret_key> --auth-key <auth_key> --auth-fail-threshold <threshold>
"""

from __future__ import annotations

import argparse
import asyncio
import logging

import aiohttp

from pyremootio import RemootioClient


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--secret-key", required=True)
    parser.add_argument("--auth-key", required=True)
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument(
        "--auth-fail-threshold",
        type=int,
        default=4,
        help="consecutive AUTH failures before listen_auth_failure fires (default: 10)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logging.getLogger("pyremootio").setLevel(logging.INFO)

    def on_auth_failure() -> None:
        logging.error(
            "AUTH failed %s times in a row; API keys may have changed. "
            "Reconnect is still running.",
            args.auth_fail_threshold,
        )

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
            auth_fail_threshold=args.auth_fail_threshold,
        )
        client.listen_auth_failure(on_auth_failure)
        client.listen_connection(on_connection)
        await client.connect(reconnect=True)
        logging.info(
            "Running with reconnect (serial=%s, authenticated=%s)",
            client.serial_number,
            client.authenticated,
        )
        try:
            await asyncio.Event().wait()
        finally:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
