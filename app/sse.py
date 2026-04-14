import asyncio
import json
from typing import AsyncGenerator, Optional

from app.models import SSEEvent

# session_id -> asyncio.Queue[SSEEvent]
_queues: dict[str, asyncio.Queue] = {}
# session_id -> asyncio.Event (set = cancelled)
_cancel_events: dict[str, asyncio.Event] = {}


def create_session(session_id: str) -> tuple[asyncio.Queue, asyncio.Event]:
    q: asyncio.Queue = asyncio.Queue()
    ev = asyncio.Event()
    _queues[session_id] = q
    _cancel_events[session_id] = ev
    return q, ev


def get_queue(session_id: str) -> Optional[asyncio.Queue]:
    return _queues.get(session_id)


def get_cancel_event(session_id: str) -> Optional[asyncio.Event]:
    return _cancel_events.get(session_id)


def cancel_session(session_id: str) -> bool:
    ev = _cancel_events.get(session_id)
    if ev:
        ev.set()
        return True
    return False


def remove_session(session_id: str) -> None:
    _queues.pop(session_id, None)
    _cancel_events.pop(session_id, None)


async def event_generator(session_id: str) -> AsyncGenerator[str, None]:
    """Async generator for FastAPI StreamingResponse (SSE)."""
    q = get_queue(session_id)
    if q is None:
        error_event = SSEEvent(event="error", data={"msg": "session not found"})
        yield f"data: {error_event.model_dump_json()}\n\n"
        return

    while True:
        try:
            event: SSEEvent = await asyncio.wait_for(q.get(), timeout=30.0)
        except asyncio.TimeoutError:
            # Send a keepalive comment so the browser connection stays open
            yield ": keepalive\n\n"
            continue

        yield f"data: {event.model_dump_json()}\n\n"

        if event.event in ("done", "error"):
            remove_session(session_id)
            break
