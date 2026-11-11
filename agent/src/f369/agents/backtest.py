"""Backtest Agent — tests identical detection logic on historical data.

Reuses the deterministic detection engine so backtest and live detection are
identical. Emits BacktestResult metrics to the Central Engine for the
Data Analysis Agent.
"""

from __future__ import annotations

from typing import Any, Dict, List

from ..bus import MessageBus
from ..detection import detect_cisd, detect_fvg, detect_smr, detect_smt
from ..engine import engine
from ..models import Candle, Instrument, PatternType, Timeframe
from ..sync import synchronizer
from .base import BaseAgent


class BacktestAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        super().__init__("backtest_agent", bus)

    async def tick(self) -> None:
        self.refresh_context()
        await self._run_backtest()

    async def _run_backtest(self) -> None:
        nq = synchronizer.nq_candles.get(Timeframe.M1, [])
        es = synchronizer.es_candles.get(Timeframe.M1, [])
        if len(nq) < 50 or len(es) < 50:
            return

        # Deterministic detection on historical windows (same code path as live).
        smt = detect_smt(nq[-50:], es[-50:])
        smr = detect_smr(nq[-50:], es[-50:])
        fvg = detect_fvg(nq[-20:])

        signals_detected = sum(
            1
            for s in (smt, smr, fvg)
            if s.status.__class__.__name__ == "ConfirmationStatus"
            and getattr(s, "confirmed", False)
        )

        metrics = {
            "window_candles": len(nq),
            "smt_confirmed": smt.confirmed,
            "smr_confirmed": smr.confirmed,
            "fvg_confirmed": fvg.confirmed,
            "signals_detected": signals_detected,
            "smr_divergence": smr.divergence,
            "fvg_type": fvg.fvg_type,
        }

        await self.publish(
            "backtest.result",
            {"metrics": metrics, "detector": "deterministic", "symbols": ["NQ", "ES"]},
        )

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "Backtest Agent",
            "role": "backtesting",
            "responsibility": "Run identical detection logic on historical data to validate edge.",
            "output": "BacktestResult -> Central Engine -> Data Analysis Agent",
            "enabled": self.enabled,
        }
