"""Live Agent — real-time market monitoring of NQ and ES.

Identifies price-action signatures (liquidity sweeps, SMT, SMR, CISD, FVG/iFVG)
using the deterministic detection engine and feeds PatternDetected signals to the
Central Engine. Never makes a trade decision.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..bus import MessageBus
from ..detection import detect_cisd, detect_fvg, detect_ifvg, detect_smr, detect_smt
from ..engine import engine
from ..models import Candle, Instrument as InstEnum, PatternType, Timeframe
from ..sync import AlignedPair, synchronizer
from .base import BaseAgent


class LiveAgent(BaseAgent):
    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        super().__init__("live_agent", bus)
        self._nq_window: list = []
        self._es_window: list = []
        self._active_setup_id: Optional[str] = None

    async def tick(self) -> None:
        self.refresh_context()
        await self._process_latest_candles()

    async def _process_latest_candles(self) -> None:
        nq_list = synchronizer.nq_candles.get(Timeframe.M1, [])
        es_list = synchronizer.es_candles.get(Timeframe.M1, [])
        if not nq_list or not es_list:
            return

        latest_nq = nq_list[-1]
        latest_es = es_list[-1]

        self._nq_window.append(latest_nq)
        self._es_window.append(latest_es)
        if len(self._nq_window) > 200:
            self._nq_window = self._nq_window[-200:]
        if len(self._es_window) > 200:
            self._es_window = self._es_window[-200:]

        setup = self._ensure_setup()
        # --- Liquidity sweep + SMT ---
        smt = detect_smt(self._nq_window, self._es_window)
        if smt.status in {smt.status.__class__.CONFIRMED, smt.status.__class__.REJECTED}:
            await self.publish(
                "pattern.detected",
                {
                    "setup_id": setup.setup_id,
                    "pattern_type": PatternType.SMT.value,
                    "instrument": Instrument.NQ.value,
                    "timeframe": Timeframe.M1.value,
                    "timestamp": latest_nq.timestamp.isoformat(),
                    "price_levels": {
                        "nq_level": smt.nq_level,
                        "es_level": smt.es_level,
                    },
                    "detection_criteria": smt.criteria,
                    "confirmation_status": smt.status.value,
                    "metadata": {
                        "took_liquidity": smt.instrument_took_liquidity.value if smt.instrument_took_liquidity else None,
                        "failed_liquidity": smt.instrument_failed.value if smt.instrument_failed else None,
                    },
                },
            )

        # --- SMR (1-minute swing divergence) ---
        smr = detect_smr(self._nq_window, self._es_window)
        if smr.confirmed:
            await self.publish(
                "pattern.detected",
                {
                    "setup_id": setup.setup_id,
                    "pattern_type": PatternType.SMR.value,
                    "instrument": Instrument.NQ.value,
                    "timeframe": Timeframe.M1.value,
                    "timestamp": latest_nq.timestamp.isoformat(),
                    "price_levels": {
                        "nq_swing_high": smr.nq_swing_high,
                        "nq_swing_low": smr.nq_swing_low,
                        "es_swing_high": smr.es_swing_high,
                        "es_swing_low": smr.es_swing_low,
                    },
                    "detection_criteria": smr.criteria,
                    "confirmation_status": smr.status.value,
                    "metadata": {"divergence": smr.divergence},
                },
            )

        # --- CISD ---
        cisd = self._detect_cisd_from_window()
        if cisd and cisd.confirmed:
            await self.publish(
                "pattern.detected",
                {
                    "setup_id": setup.setup_id,
                    "pattern_type": PatternType.CISD.value,
                    "instrument": Instrument.NQ.value,
                    "timeframe": Timeframe.M1.value,
                    "timestamp": latest_nq.timestamp.isoformat(),
                    "price_levels": {
                        "break_level": cisd.break_level,
                        "break_price": cisd.break_price,
                    },
                    "detection_criteria": cisd.criteria,
                    "confirmation_status": cisd.status.value,
                    "metadata": {},
                },
            )

        # --- FVG / iFVG ---
        fvg = detect_fvg(self._nq_window)
        if fvg.confirmed:
            pattern_type = PatternType.iFVG if fvg.mitigation_status == "inverted" else PatternType.FVG
            await self.publish(
                "pattern.detected",
                {
                    "setup_id": setup.setup_id,
                    "pattern_type": pattern_type.value,
                    "instrument": Instrument.NQ.value,
                    "timeframe": Timeframe.M1.value,
                    "timestamp": latest_nq.timestamp.isoformat(),
                    "price_levels": {"top": fvg.top, "bottom": fvg.bottom},
                    "detection_criteria": fvg.criteria,
                    "confirmation_status": fvg.status.value,
                    "metadata": {
                        "fvg_type": fvg.fvg_type,
                        "mitigation_status": fvg.mitigation_status,
                    },
                },
            )

    def _ensure_setup(self):
        ctx = self.context
        if ctx and ctx.active_setups():
            return ctx.active_setups()[-1]
        setup = engine.create_setup(Instrument.NQ)
        self._active_setup_id = setup.setup_id
        return setup

    def _detect_cisd_from_window(self):
        if len(self._nq_window) < 2:
            return None
        opposing = self._nq_window[-2]
        break_candle = self._nq_window[-1]
        return detect_cisd(self._nq_window, opposing, break_candle)

    def describe(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": "Live Agent",
            "role": "live_monitoring",
            "responsibility": "Monitor NQ and ES in real time; detect liquidity sweeps, SMT, SMR, CISD, FVG/iFVG.",
            "output": "PatternDetected signals -> Central Engine",
            "enabled": self.enabled,
        }

