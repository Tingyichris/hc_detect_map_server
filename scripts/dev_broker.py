"""Tiny local MQTT broker for development when Docker/Mosquitto isn't available.

    python scripts/dev_broker.py

Uses amqtt (pure Python, MQTT 3.1.1). The real deployment uses Mosquitto — clients speak
plain MQTT 3.1.1 either way.
"""
from __future__ import annotations

import asyncio
import logging
import sys

from amqtt.broker import Broker

CONFIG = {
    "listeners": {"default": {"type": "tcp", "bind": "0.0.0.0:1883"}},
    "sys_interval": 10,
    "auth": {"allow-anonymous": True, "plugins": ["auth_anonymous"]},
    "topic-check": {"enabled": False},
}


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s broker: %(message)s")
    broker = Broker(CONFIG)
    await broker.start()
    logging.info("MQTT broker listening on 0.0.0.0:1883")
    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, asyncio.CancelledError):
        await broker.shutdown()


if __name__ == "__main__":
    if sys.platform == "win32":
        # Match the clients: SelectorEventLoop (Proactor lacks add_reader/add_writer).
        try:
            asyncio.run(main(), loop_factory=asyncio.SelectorEventLoop)
        except TypeError:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
            asyncio.run(main())
    else:
        asyncio.run(main())
