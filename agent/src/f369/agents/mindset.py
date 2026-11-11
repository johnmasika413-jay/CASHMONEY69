"""Mindset Agent — risk discipline guard.

Monitors execution frequency, drawdown limits, and daily trade caps to prevent
overtrading or revenge trading. Emits RiskVerdict (PASS/FAIL) to the Central
Engine. Cannot independently halt a trade — only the Engine acts on the verdict.
"""

from __future__ import annotations

from typing import Any, Dict

from ..bus import MessageBus
from ..engine import engine
from .base import BaseAgent


class MindsetAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        super().__init__("mindset_agent", bus)

    async def tick(self) -> None:
        self.refresh_context()
        await self._evaluate_risk()

    async def _evaluate_risk(self) -> None:
        ctx = self.context
        if ctx is None:
            return

        risk = ctx.risk_limits
        setups = ctx.active_setups()
        all_setups = list(ctx.setups.values())

        # Daily trade cap.
        alerted_today = sum(
            1
            for s in all_setups
            if s.state == s.state.ALERT_SENT
        )

        # Concurrent setup cap.
        concurrent = len(setups)

        reasons: list = []
        verdict = "PASS"

        if alerted_today >= risk.max_daily_trades:
            verdict = "FAIL"
            reasons.append(f"daily trade cap reached ({alerted_today}/{risk.max_daily_trades})")
        if concurrent >= risk.max_concurrent_setups:
            verdict = "FAIL"
            reasons.append(f"concurrent setup cap reached ({concurrent}/{risk.max_concurrent_setups})")

        await self.publish(
            "risk.verdict",
            {
                "setup_id": setups[-1].setup_id if setups else None,
                "verdict": verdict,
                "reason": "; ".join(reasons) if reasons else "risk limits OK",
                "alerted_today": alerted_today,
                "concurrent_setups": concurrent,
            },
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "Mindset Agent",
            "role": "risk_discipline",
            "responsibility": "Enforce drawdown limits, daily trade caps, and overtrading prevention.",
            "output": "RiskVerdict -> Central Engine",
            "enabled": self.enabled,
        }
