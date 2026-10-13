"""asyncio.run() that works on Windows.

Windows' default ProactorEventLoop does not implement add_reader/add_writer, which paho-mqtt
(under aiomqtt) requires. Force a SelectorEventLoop there. No-op everywhere else.
"""
from __future__ import annotations

import asyncio
import sys
from collections.abc import Coroutine
from typing import Any


def run(coro: Coroutine[Any, Any, Any]) -> Any:
    if sys.platform != "win32":
        return asyncio.run(coro)
    try:
        return asyncio.run(coro, loop_factory=asyncio.SelectorEventLoop)  # py3.12+
    except TypeError:  # pragma: no cover - older Python on Windows
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        return asyncio.run(coro)
