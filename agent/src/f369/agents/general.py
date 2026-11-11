"""General Agent — establishes the daily directional bias using D, 1H, and 15m charts.

Emits a BiasEstablished signal to the Central Engine. Does not make trade decisions.
"""

from __future__ import annotations

from typing import Any, Dict

from ..bus import MessageBus
from ..config import Instrument
from ..engine import engine
from ..models import Candle, Direction, Timeframe
from ..sync import synchronizer
from .base import BaseAgent


class GeneralAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        super().__init__("general_agent", bus)

    async def tick(self) -> None:
        self.refresh_context()
        await self._evaluate_bias()

    async def _evaluate_bias(self) -> None:
        """Multi-timeframe alignment: Daily, 1H, 15m must agree for a strong bias."""
        d_candles = synchronizer.nq_candles.get(Timeframe.D1, [])
        h1_candles = synchronizer.nq_candles.get(Timeframe.H1, [])
        m15_candles = synchronizer.nq_candles.get(Timeframe.M15, [])

        if len(d_candles) < 2 or len(h1_candles) < 2 or len(m15_candles) < 2:
            return  # insufficient data — do not fabricate a bias

        d = d_candles[-2]  # prior completed daily
        h1 = h1_candles[-2]
        m15 = m15_candles[-2]

        alignment: Dict[Timeframe, Direction] = {
            Timeframe.D1: Direction.LONG if d.close >= d.open else Direction.SHORT,
            Timeframe.H1: Direction.LONG if h1.close >= h1.open else Direction.SHORT,
            Timeframe.M15: Direction.LONG if m15.close >= m15.open else Direction.SHORT,
        }

        directions = list(alignment.values())
        if directions.count(directions[0]) == len(directions):
            direction = directions[0]
        else:
            # Mixed timeframes — no tradable bias until alignment.
            direction = Direction.FLAT

        key_levels: Dict[str, float] = {}
        # HTF liquidity / imbalance levels from the daily swing points.
        from ..detection import find_swing_points
        highs, lows = find_swing_points(d_candles)
        if highs:
            key_levels["daily_swing_high"] = highs[-1].high
        if lows:
            key_levels["daily_swing_low"] = lows[-1].low

        confidence = 1.0 if direction != Direction.FLAT else 0.0

        await self.publish(
            "bias.established",
            {
                "setup_id": None,  # engine will create/attach
                "direction": direction.value,
                "timeframe_alignment": {k.value: v.value for k, v in alignment.items()},
                "key_levels": key_levels,
                "confidence": confidence,
            },
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "General Agent",
            "role": "directional_bias",
            "responsibility": "Establish daily directional bias using D, 1H, 15m multi-timeframe analysis.",
            "output": "BiasEstablished signal -> Central Engine",
            "enabled": self.enabled,
        }
