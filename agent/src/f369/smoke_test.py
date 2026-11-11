"""Smoke test: simulates the full F369 execution workflow end-to-end.

1. General Agent bias -> MONITORING
2. Live Agent SMT -> ACTIVE_WATCH
3. Live Agent SMR -> SMR_CONFIRMED (verified vs bias)
4. Live Agent CISD -> CISD_CONFIRMED
5. Live Agent FVG/iFVG -> ENTRY_READY
6. Mindset PASS + final validation -> ALERT_SENT -> Alerts Agent notified
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from f369.config import CONFIG
from f369.detection import detect_cisd, detect_fvg, detect_smr, detect_smt
from f369.engine import engine
from f369.models import (
    Candle,
    ConfirmationStatus,
    Direction,
    Instrument,
    PatternType,
    SetupState,
    Timeframe,
)


def _utc(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 1, 15, hour, minute, second, tzinfo=timezone.utc)


def make_candles(instrument, opens, highs, lows, closes, start):
    return [
        Candle(
            instrument=instrument,
            timeframe=Timeframe.M1,
            timestamp=start + timedelta(minutes=i),
            open=o,
            high=h,
            low=l,
            close=c,
            volume=1000,
        )
        for i, (o, h, l, c) in enumerate(zip(opens, highs, lows, closes))
    ]


def test_detection() -> None:
    print("=== Deterministic Detection Tests ===")
    base = _utc(9, 30)

    # ---- SMT: NQ pierces ITS swing-high liquidity; ES does not pierce ITS own ----
    nq_highs = [20000, 20005, 19995, 20020, 20000, 20005, 19998, 20000, 20005, 20025]
    nq_lows = [19990] * 10
    nq_opens = [20000] * 10
    nq_closes = [20005] * 10
    nq_closes[-1] = 20022
    nq = make_candles(Instrument.NQ, nq_opens, nq_highs, nq_lows, nq_closes, base)

    es_highs = [4000, 4005, 3995, 4010, 4000, 4005, 3998, 4000, 4005, 4001]
    es_lows = [3990] * 10
    es_opens = [4000] * 10
    es_closes = [4001] * 10
    es = make_candles(Instrument.ES, es_opens, es_highs, es_lows, es_closes, base)

    smt = detect_smt(nq, es)
    assert smt.status == ConfirmationStatus.CONFIRMED, smt.criteria
    assert smt.instrument_took_liquidity == Instrument.NQ, smt.criteria
    assert smt.instrument_failed == Instrument.ES, smt.criteria
    print(f"  SMT: {smt.criteria} -> {smt.status.value} OK")

    # ---- SMR: NQ higher-high vs ES lower-high (opposing swing directions) ----
    nq2_highs = [20000, 20005, 19995, 20020, 20000, 20005, 19998,
                 20000, 20005, 19998, 20030, 20000, 20005, 19998]
    nq2_lows = [19990] * 14
    nq2_opens = [20000] * 14
    nq2_closes = [20005] * 14
    nq2 = make_candles(Instrument.NQ, nq2_opens, nq2_highs, nq2_lows, nq2_closes, base)

    es2_highs = [4000, 4005, 3995, 4010, 4000, 4005, 3998,
                 4000, 4000, 3998, 4005, 4000, 4000, 3998]
    es2_lows = [3990] * 14
    es2_opens = [4000] * 14
    es2_closes = [4001] * 14
    es2 = make_candles(Instrument.ES, es2_opens, es2_highs, es2_lows, es2_closes, base)

    smr = detect_smr(nq2, es2)
    assert smr.confirmed, smr.criteria
    assert smr.divergence == "bearish", smr.divergence
    print(f"  SMR: {smr.criteria} -> {smr.status.value} OK")

    # ---- CISD (wick rule; default config cisd_require_body_close=False) ----
    c_opens = [20000 + i for i in range(8)]
    c_highs = [20010 + i for i in range(8)]
    c_lows = [19990 + i for i in range(8)]
    c_closes = [20005 + i for i in range(8)]
    c_highs[6] = 20070   # opposing swing candle high
    c_closes[6] = 20065
    c_highs[7] = 20075   # break candle wick pierces opposing high
    c_closes[7] = 20072   # break candle body closes above opposing high (body-close rule)
    candles = make_candles(Instrument.NQ, c_opens, c_highs, c_lows, c_closes, base)
    cisd = detect_cisd(candles, opposing_candle=candles[6], break_candle=candles[7])
    assert cisd.confirmed, cisd.criteria
    assert cisd.break_level == 20070.0
    print(f"  CISD (wick): {cisd.criteria} -> {cisd.status.value} OK")

    # ---- FVG: bullish three-candle gap ----
    fvg_candles = make_candles(
        Instrument.NQ,
        opens=[20000, 20010, 20020],
        highs=[20005, 20015, 20030],
        lows=[19990, 20000, 20010],
        closes=[20005, 20010, 20025],
        start=base,
    )
    fvg = detect_fvg(fvg_candles)
    assert fvg.confirmed, fvg.criteria
    assert fvg.fvg_type == "bullish", fvg.fvg_type
    print(f"  FVG: {fvg.criteria} -> {fvg.status.value} OK")

    print("  All detection tests passed.\n")

async def test_workflow() -> None:
    print("=== Execution Workflow Test ===")

    # 1. General bias -> MONITORING
    await engine.bus.publish(
        "bias.established",
        source_agent="general_agent",
        payload={
            "direction": Direction.LONG.value,
            "timeframe_alignment": {"1d": "long", "1h": "long", "15m": "long"},
            "key_levels": {"daily_swing_high": 20050.0, "daily_swing_low": 19950.0},
            "confidence": 1.0,
        },
    )
    setup = engine.active_setups()[0]
    assert setup.state == SetupState.MONITORING, setup.state
    print(f"  Step 1: bias established -> state={setup.state.value}")

    # 2. SMT -> ACTIVE_WATCH
    await engine.bus.publish(
        "pattern.detected",
        source_agent="live_agent",
        payload={
            "setup_id": setup.setup_id,
            "pattern_type": PatternType.SMT.value,
            "instrument": Instrument.NQ.value,
            "timeframe": Timeframe.M1.value,
            "timestamp": _utc(9, 30).isoformat(),
            "price_levels": {"nq_level": 20020.0, "es_level": 4010.0},
            "detection_criteria": "NQ pierced liquidity; ES failed",
            "confirmation_status": ConfirmationStatus.CONFIRMED.value,
            "metadata": {"took_liquidity": "NQ", "failed_liquidity": "ES"},
        },
    )
    setup = engine.get_setup(setup.setup_id)
    assert setup.state == SetupState.ACTIVE_WATCH, setup.state
    print(f"  Step 2: SMT confirmed -> state={setup.state.value}")

    # 3. SMR -> SMR_CONFIRMED (bias exists)
    await engine.bus.publish(
        "pattern.detected",
        source_agent="live_agent",
        payload={
            "setup_id": setup.setup_id,
            "pattern_type": PatternType.SMR.value,
            "instrument": Instrument.NQ.value,
            "timeframe": Timeframe.M1.value,
            "timestamp": _utc(9, 31).isoformat(),
            "price_levels": {"nq_swing_high": 20030.0, "es_swing_high": 4005.0},
            "detection_criteria": "NQ higher-high vs ES lower-high — SMR divergence",
            "confirmation_status": ConfirmationStatus.CONFIRMED.value,
            "metadata": {"divergence": "bearish"},
        },
    )
    setup = engine.get_setup(setup.setup_id)
    assert setup.state == SetupState.SMR_CONFIRMED, setup.state
    print(f"  Step 3: SMR confirmed -> state={setup.state.value}")

    # 4. CISD -> CISD_CONFIRMED
    await engine.bus.publish(
        "pattern.detected",
        source_agent="live_agent",
        payload={
            "setup_id": setup.setup_id,
            "pattern_type": PatternType.CISD.value,
            "instrument": Instrument.NQ.value,
            "timeframe": Timeframe.M1.value,
            "timestamp": _utc(9, 32).isoformat(),
            "price_levels": {"break_level": 20070.0, "break_price": 20075.0},
            "detection_criteria": "CISD wick break of opposing candle high",
            "confirmation_status": ConfirmationStatus.CONFIRMED.value,
        },
    )
    setup = engine.get_setup(setup.setup_id)
    assert setup.state == SetupState.CISD_CONFIRMED, setup.state
    print(f"  Step 4: CISD confirmed -> state={setup.state.value}")

    # 5. FVG -> ENTRY_READY
    await engine.bus.publish(
        "pattern.detected",
        source_agent="live_agent",
        payload={
            "setup_id": setup.setup_id,
            "pattern_type": PatternType.FVG.value,
            "instrument": Instrument.NQ.value,
            "timeframe": Timeframe.M1.value,
            "timestamp": _utc(9, 33).isoformat(),
            "price_levels": {"top": 20030.0, "bottom": 20005.0},
            "detection_criteria": "Bullish FVG gap [20005.00, 20030.00]",
            "confirmation_status": ConfirmationStatus.CONFIRMED.value,
        },
    )
    setup = engine.get_setup(setup.setup_id)
    assert setup.state == SetupState.ENTRY_READY, setup.state
    print(f"  Step 5: FVG entry -> state={setup.state.value}")

    # 6. Mindset PASS + final validation -> ALERT_SENT -> alert fires
    alerted = []
    engine.set_alert_handler(lambda alert: alerted.append(alert))

    await engine.bus.publish(
        "risk.verdict",
        source_agent="mindset_agent",
        payload={
            "setup_id": setup.setup_id,
            "verdict": "PASS",
            "reason": "risk limits OK",
        },
    )
    setup = engine.get_setup(setup.setup_id)
    assert setup.state == SetupState.ALERT_SENT, setup.state
    assert len(alerted) == 1, f"expected 1 alert, got {len(alerted)}"
    print(f"  Step 6: Mindset PASS -> ALERT_SENT -> alert fired for setup {setup.setup_id}")
    print(f"  Alert payload keys: {sorted(alerted[0].payload.keys())}")

    print("  Full workflow passed.\n")


def main() -> None:
    assert CONFIG.cisd.structure_break_require_body_close is True
    test_detection()
    asyncio.run(test_workflow())
    print("SMOKE TEST PASSED — F369 Central Intelligence architecture is operational.")


if __name__ == "__main__":
    main()

