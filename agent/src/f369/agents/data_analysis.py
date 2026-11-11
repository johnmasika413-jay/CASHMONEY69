"""Data Analysis Agent — processes historical performance and setup records.

Reads shared context (read-only), computes real-time metrics, maintains setup
records, and publishes PerformanceSnapshot to the Central Engine.
"""

from __future__ import annotations

from typing import Any, Dict

from ..bus import MessageBus
from ..engine import engine
from .base import BaseAgent


class DataAnalysisAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        super().__init__("data_analysis_agent", bus)

    async def tick(self) -> None:
        self.refresh_context()
        await self._publish_snapshot()

    async def _publish_snapshot(self) -> None:
        ctx = self.context
        if ctx is None:
            return

        setups = list(ctx.setups.values())
        total = len(setups)
        alerted = sum(1 for s in setups if s.state == s.state.ALERT_SENT)
        invalidated = sum(1 for s in setups if s.state == s.state.INVALIDATED)
        active = len(ctx.active_setups())

        metrics = {
            "setup_total": total,
            "setup_alerted": alerted,
            "setup_invalidated": invalidated,
            "setup_active": active,
            "pattern_history_len": len(ctx.pattern_history),
        }

        await self.publish(
            "performance.snapshot",
            {"metrics": metrics, "source": "data_analysis_agent"},
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "Data Analysis Agent",
            "role": "performance_tracking",
            "responsibility": "Process historical performance, track real-time metrics, maintain setup records.",
            "output": "PerformanceSnapshot -> Central Engine",
            "enabled": self.enabled,
        }
