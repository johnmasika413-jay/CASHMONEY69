"""Async message bus for the F369 Central Intelligence Engine.

Agents publish signals to the bus; the Central Engine subscribes and routes
them through the validation state machine. Agents never talk to each other
directly.
"""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .models import Message


@dataclass
class BusMessage:
    topic: str
    message: Message


class MessageBus:
    """Topic-based async pub/sub bus. Agents publish; the Engine consumes."""

    def __init__(self) -> None:
        self._subscribers: Dict[str, List[Callable[[BusMessage], Awaitable[None]]]] = defaultdict(list)
        self._history: List[BusMessage] = []
        self._lock = asyncio.Lock()

    def subscribe(self, topic: str, handler: Callable[[BusMessage], Awaitable[None]]) -> None:
        self._subscribers[topic].append(handler)

    async def publish(self, topic: str, source_agent: str, payload: Dict[str, Any]) -> str:
        msg_id = str(uuid.uuid4())
        msg = Message(message_id=msg_id, source_agent=source_agent, timestamp=datetime.utcnow(), payload=payload)
        bus_msg = BusMessage(topic=topic, message=msg)
        async with self._lock:
            self._history.append(bus_msg)
        handlers = list(self._subscribers.get(topic, []))
        if not handlers:
            # No subscriber — still recorded for audit, but no one acts.
            return msg_id
        await asyncio.gather(*(h(bus_msg) for h in handlers), return_exceptions=True)
        return msg_id

    async def request_shared_context(self) -> Dict[str, Any]:
        """Placeholder hook for agents to ask the Engine for a read-only snapshot."""
        raise NotImplementedError("Agents obtain SharedContext via the Engine's context provider.")

    def history(self, topic: Optional[str] = None) -> List[BusMessage]:
        if topic is None:
            return list(self._history)
        return [m for m in self._history if m.topic == topic]
