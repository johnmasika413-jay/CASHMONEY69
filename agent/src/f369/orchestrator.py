"""F369 Orchestrator — event-driven loop for the Central Intelligence swarm.

Wires the six agents to the Central Engine via the message bus and drives the
deterministic detection tick on every loop iteration.
"""

from __future__ import annotations

import asyncio
import signal
from typing import Any, Dict, List, Optional

from ..engine import engine
from ..models import SharedContext
from ..sync import synchronizer
from .agents.alerts import AlertsAgent
from .agents.backtest import BacktestAgent
from .agents.base import BaseAgent
from .agents.data_analysis import DataAnalysisAgent
from .agents.general import GeneralAgent
from .agents.live import LiveAgent
from .agents.mindset import MindsetAgent


class F369Orchestrator:
    def __init__(self, tick_interval_seconds: float = 1.0) -> None:
        self.tick_interval = tick_interval_seconds
        self._agents: List[BaseAgent] = []
        self._running = False
        self._task: Optional[asyncio.Task] = None

    def register_agent(self, agent: BaseAgent) -> None:
        self._agents.append(agent)

    def register_all_default(self) -> None:
        self.register_agent(GeneralAgent(engine.bus))
        self.register_agent(LiveAgent(engine.bus))
        self.register_agent(BacktestAgent(engine.bus))
        self.register_agent(DataAnalysisAgent(engine.bus))
        self.register_agent(MindsetAgent(engine.bus))
        self.register_agent(AlertsAgent(engine.bus))

        # Wire Mindset risk provider and Alerts handler into the engine.
        engine.set_risk_provider(lambda: engine.get_shared_context().risk_limits)

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        for agent in self._agents:
            agent.enable()

        loop = asyncio.get_running_loop()
        stop = asyncio.Event()

        def _handle_signal() -> None:
            stop.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_signal)
            except NotImplementedError:
                pass  # Windows

        self._task = asyncio.create_task(self._loop(stop))
        await self._task

    async def _loop(self, stop: asyncio.Event) -> None:
        while not stop.is_set() and self._running:
            # Refresh engine shared context snapshot once per tick.
            engine.get_shared_context()

            # Tick every agent (each publishes signals to the bus; engine consumes).
            await asyncio.gather(
                *(agent.tick() for agent in self._agents if agent.enabled),
                return_exceptions=True,
            )

            try:
                await asyncio.wait_for(stop.wait(), timeout=self.tick_interval)
            except asyncio.TimeoutError:
                continue

    def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    def status(self) -> Dict[str, Any]:
        return {
            "running": self._running,
            "agents": [a.describe() for a in self._agents],
            "engine": {
                "setups": len(engine.setups),
                "active_setups": len(engine.active_setups()),
                "active_bias": engine.active_bias.payload if engine.active_bias else None,
                "pattern_history": len(engine.pattern_history),
            },
        }


async def run(tick_interval_seconds: float = 1.0) -> None:
    orch = F369Orchestrator(tick_interval_seconds=tick_interval_seconds)
    orch.register_all_default()
    await orch.start()
