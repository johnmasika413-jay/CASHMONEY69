"""Base agent for the F369 Central Intelligence swarm.

Agents:
- receive a read-only SharedContext (cannot mutate engine state)
- publish structured signals to the MessageBus (never trade decisions)
- expose a tick() method called by the orchestrator on each event loop iteration
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from ..bus import MessageBus
from ..config import CONFIG
from ..engine import engine
from ..models import Instrument, SharedContext
from ..sync import synchronizer


class BaseAgent(ABC):
    def __init__(self, agent_id: str, bus: Optional[MessageBus] = None) -> None:
        self.agent_id = agent_id
        self.bus = bus or engine.bus
        self._context: Optional[SharedContext] = None
        self._last_tick: Optional[datetime] = None
        self._enabled = True
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def enable(self) -> None:
        self._enabled = True

    def disable(self) -> None:
        self._enabled = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Shared context (read-only)
    # ------------------------------------------------------------------
    def refresh_context(self) -> SharedContext:
        self._context = engine.get_shared_context()
        return self._context

    @property
    def context(self) -> Optional[SharedContext]:
        return self._context

    # ------------------------------------------------------------------
    # Messaging helpers
    # ------------------------------------------------------------------
    async def publish(self, topic: str, payload: Dict[str, Any]) -> str:
        return await self.bus.publish(topic, source_agent=self.agent_id, payload=payload)

    # ------------------------------------------------------------------
    # Abstract API
    # ------------------------------------------------------------------
    @abstractmethod
    async def tick(self) -> None:
        """Called by the orchestrator on every loop iteration."""

    @abstractmethod
    def describe(self) -> Dict[str, Any]:
        """Human-readable agent description for the dashboard / Pixel Office."""
