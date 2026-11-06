"""Server-Sent Events, built on plain HTTP streaming.

Why SSE rather than WebSockets? The data flows one way (server → browser), SSE
reconnects and resumes for free (``Last-Event-ID``), and it passes through every HTTP
proxy. WebSockets would add a second protocol for no gain.

The streaming loop is a pure async generator driven by a ``source`` callable, so its
behaviour (heartbeats, max duration, disconnects, completion) is unit-tested without
HTTP, a database or real time.

Production note: proxies buffer responses by default. ``X-Accel-Buffering: no``
disables that for nginx, and heartbeats (comment lines every 15s) stop idle-timeout
disconnects on load balancers.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from fastapi.responses import StreamingResponse

RECONNECT_MS = 3000
HEARTBEAT_SECONDS = 15.0


@dataclass(frozen=True, slots=True)
class SSEEvent:
    event: str
    data: Any
    id: str | None = None

    def encode(self) -> str:
        lines = []
        if self.id is not None:
            lines.append(f"id: {self.id}")
        lines.append(f"event: {self.event}")
        # json.dumps never emits raw newlines, so one data line is always enough.
        lines.append(f"data: {json.dumps(self.data, default=str, separators=(',', ':'))}")
        return "\n".join(lines) + "\n\n"


# A source returns (events to send now, finished?).
Source = Callable[[], Awaitable[tuple[list[SSEEvent], bool]]]


async def event_stream(
    source: Source,
    *,
    is_disconnected: Callable[[], Awaitable[bool]],
    poll_seconds: float,
    max_seconds: float,
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> AsyncIterator[str]:
    yield f"retry: {RECONNECT_MS}\n\n"
    started = last_sent = monotonic()
    while True:
        events, finished = await source()
        for event in events:
            yield event.encode()
            last_sent = monotonic()
        if finished:
            yield SSEEvent("end", {"reason": "finished"}).encode()
            return
        if monotonic() - started >= max_seconds:
            # Bounded streams: the client reconnects with Last-Event-ID and resumes.
            # This caps the server-side resources any one client can hold.
            yield SSEEvent("end", {"reason": "max_duration"}).encode()
            return
        if monotonic() - last_sent >= heartbeat_seconds:
            yield ": keepalive\n\n"
            last_sent = monotonic()
        if await is_disconnected():
            return
        await sleep(poll_seconds)


def sse_response(stream: AsyncIterator[str]) -> StreamingResponse:
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
