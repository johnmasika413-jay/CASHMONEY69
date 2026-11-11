"""Alerts Agent — outbound notifications.

Fires ONLY after the Central Engine publishes alert.authorized, which happens
only after absolute validation (bias + SMT + SMR + CISD + FVG/iFVG + Mindset PASS).
Never sends a signal on its own initiative.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from ..bus import MessageBus
from .base import BaseAgent


class AlertsAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None, notifier: Optional[Callable[[Dict[str, Any]], Any]] = None) -> None:
        super().__init__("alerts_agent", bus)
        self.notifier = notifier or self._default_notifier
        self._subscribed = False

    async def tick(self) -> None:
        self.refresh_context()
        if not self._subscribed and self.bus:
            self.bus.subscribe("alert.authorized", self._on_alert_authorized)
            self._subscribed = True

    async def _on_alert_authorized(self, bus_msg) -> None:
        payload = bus_msg.message.payload
        await self.notifier(payload)

    async def _default_notifier(self, payload: Dict[str, Any]) -> None:
        """Placeholder: replace with email/Slack/Telegram/Discord webhook."""
        print(f"[ALERT] Setup {payload.get('setup_id')} validated — {payload.get('instrument')}")
        print(f"[ALERT] Bias: {payload.get('bias')}")
        print(f"[ALERT] SMT: {payload.get('smt')}")
        print(f"[ALERT] SMR: {payload.get('smr')}")
        print(f"[ALERT] CISD: {payload.get('cisd')}")
        print(f"[ALERT] FVG: {payload.get('fvg')}")
        print(f"[ALERT] Validated at: {payload.get('validated_at')}")

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "Alerts Agent",
            "role": "notifications",
            "responsibility": "Send outbound notification only after Central Engine absolute validation.",
            "output": "Outbound alert (email/Slack/webhook)",
            "enabled": self.enabled,
        }
