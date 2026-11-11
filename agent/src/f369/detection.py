"""Deterministic price-action detection engine.

All detection is mathematical: actual OHLCV prices, timestamps, swing points,
and NQ/ES correlation. The AI explains these results; it never guesses whether
a pattern exists.

Undefined strategy rules are read from CONFIG.cisd, CONFIG.smt, CONFIG.smr,
and CONFIG.fvg and must be confirmed before production use.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Tuple

from .config import CONFIG
from .models import (
    Candle,
    ConfirmationStatus,
    Instrument,
    PatternSignal,
    PatternType,
    Timeframe,
)


def _require(candles: List[Candle], min_count: int, label: str) -> Optional[str]:
    if len(candles) < min_count:
        return f"INSUFFICIENT_DATA: {label} requires {min_count} candles, have {len(candles)}"
    return None


def _sorted(candles: List[Candle]) -> List[Candle]:
    return sorted(candles, key=lambda c: c.timestamp)


def find_swing_points(
    candles: List[Candle], lookback: int = 3
) -> Tuple[List[Candle], List[Candle]]:
    candles = _sorted(candles)
    n = len(candles)
    lb = CONFIG.detection.swing_lookback or lookback
    if n < 2 * lb + 1:
        return [], []

    swing_highs: List[Candle] = []
    swing_lows: List[Candle] = []
    tol = CONFIG.detection.swing_tolerance

    for i in range(lb, n - lb):
        window = candles[i - lb : i + lb + 1]
        center = candles[i]
        highs = [c.high for c in window]
        lows = [c.low for c in window]
        if center.high >= max(highs) - tol and all(center.high >= h - tol for h in highs):
            swing_highs.append(center)
        if center.low <= min(lows) + tol and all(center.low <= l + tol for l in lows):
            swing_lows.append(center)
    return swing_highs, swing_lows



def liquidity_levels_from_swings(
    swing_highs: List[Candle], swing_lows: List[Candle], buffer_ticks: Optional[float] = None
) -> dict:
    buf = buffer_ticks if buffer_ticks is not None else CONFIG.detection.liquidity_buffer_ticks
    return {
        "buy_side_liquidity": [c.high + buf for c in swing_highs],
        "sell_side_liquidity": [c.low - buf for c in swing_lows],
        "swing_highs": [c.high for c in swing_highs],
        "swing_lows": [c.low for c in swing_lows],
    }


@dataclass
class SMTResult:
    confirmed: bool
    status: ConfirmationStatus
    instrument_took_liquidity: Optional[Instrument]
    instrument_failed: Optional[Instrument]
    nq_level: Optional[float]
    es_level: Optional[float]
    criteria: str
    nq_candle: Optional[Candle] = None
    es_candle: Optional[Candle] = None


def detect_smt(
    nq_candles: List[Candle], es_candles: List[Candle]
) -> SMTResult:
    err = _require(nq_candles, 2, "SMT NQ") or _require(es_candles, 2, "SMT ES")
    if err:
        return SMTResult(
            confirmed=False,
            status=ConfirmationStatus.INSUFFICIENT_DATA,
            instrument_took_liquidity=None,
            instrument_failed=None,
            nq_level=None,
            es_level=None,
            criteria=err,
        )

    nq = _sorted(nq_candles)[-1]
    es = _sorted(es_candles)[-1]

    if abs((nq.timestamp - es.timestamp).total_seconds()) > 60:
        return SMTResult(
            confirmed=False,
            status=ConfirmationStatus.INSUFFICIENT_DATA,
            instrument_took_liquidity=None,
            instrument_failed=None,
            nq_level=None,
            es_level=None,
            criteria="NQ and ES candles are not timestamp-synchronized",
            nq_candle=nq,
            es_candle=es,
        )

    nq_highs, nq_lows = find_swing_points(nq_candles)
    es_highs, es_lows = find_swing_points(es_candles)

    nq_levels = liquidity_levels_from_swings(nq_highs, nq_lows)
    es_levels = liquidity_levels_from_swings(es_highs, es_lows)

    nq_liq = nq_levels["buy_side_liquidity"]
    es_liq = es_levels["buy_side_liquidity"]

    took: Optional[Instrument] = None
    failed: Optional[Instrument] = None
    nq_level: Optional[float] = None
    es_level: Optional[float] = None
    criteria = ""

    if nq_liq and es_liq:
        nq_ref = nq_liq[-1]
        es_ref = es_liq[-1]
        nq_level, es_level = nq_ref, es_ref
        nq_took = nq.high > nq_ref
        es_took = es.high > es_ref
        if nq_took and not es_took:
            took, failed = Instrument.NQ, Instrument.ES
            criteria = (
                f"NQ pierced its liquidity {nq_ref:.2f} (took); ES did not pierce "
                f"{es_ref:.2f} (failed)"
            )
        elif es_took and not nq_took:
            took, failed = Instrument.ES, Instrument.NQ
            criteria = (
                f"ES pierced its liquidity {es_ref:.2f} (took); NQ did not pierce "
                f"{nq_ref:.2f} (failed)"
            )
        elif nq_took and es_took:
            criteria = (
                f"Both took liquidity (NQ>{nq_ref:.2f}, ES>{es_ref:.2f}) — no SMT divergence"
            )
        else:
            criteria = (
                f"Neither took liquidity (NQ<={nq_ref:.2f}, ES<={es_ref:.2f}) — no SMT"
            )

    confirmed = took is not None and failed is not None
    return SMTResult(
        confirmed=confirmed,
        status=ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED,
        instrument_took_liquidity=took,
        instrument_failed=failed,
        nq_level=nq_level,
        es_level=es_level,
        criteria=criteria,
        nq_candle=nq,
        es_candle=es,
    )



@dataclass
class SMRResult:
    confirmed: bool
    status: ConfirmationStatus
    nq_swing_high: Optional[float]
    nq_swing_low: Optional[float]
    es_swing_high: Optional[float]
    es_swing_low: Optional[float]
    divergence: Optional[str]
    criteria: str
    nq_candle: Optional[Candle] = None
    es_candle: Optional[Candle] = None


def detect_smr(
    nq_candles: List[Candle], es_candles: List[Candle]
) -> SMRResult:
    err = _require(nq_candles, 2, "SMR NQ") or _require(es_candles, 2, "SMR ES")
    if err:
        return SMRResult(
            confirmed=False,
            status=ConfirmationStatus.INSUFFICIENT_DATA,
            nq_swing_high=None,
            nq_swing_low=None,
            es_swing_high=None,
            es_swing_low=None,
            divergence=None,
            criteria=err,
        )

    nq = _sorted(nq_candles)
    es = _sorted(es_candles)

    if abs((nq[-1].timestamp - es[-1].timestamp).total_seconds()) > 60:
        return SMRResult(
            confirmed=False,
            status=ConfirmationStatus.INSUFFICIENT_DATA,
            nq_swing_high=None,
            nq_swing_low=None,
            es_swing_high=None,
            es_swing_low=None,
            divergence=None,
            criteria="NQ and ES 1m candles are not timestamp-synchronized",
            nq_candle=nq[-1],
            es_candle=es[-1],
        )

    nq_highs, nq_lows = find_swing_points(nq)
    es_highs, es_lows = find_swing_points(es)

    nq_sh = nq_highs[-1].high if nq_highs else None
    nq_sl = nq_lows[-1].low if nq_lows else None
    es_sh = es_highs[-1].high if es_highs else None
    es_sl = es_lows[-1].low if es_lows else None

    divergence = "none"
    criteria = ""
    confirmed = False

    if (
        nq_sh is not None
        and es_sh is not None
        and nq_sl is not None
        and es_sl is not None
        and len(nq_highs) >= 2
        and len(es_highs) >= 2
    ):
        nq_prev_sh = nq_highs[-2].high
        es_prev_sh = es_highs[-2].high
        nq_hh = nq_sh > nq_prev_sh
        nq_lh = nq_sh < nq_prev_sh
        es_hh = es_sh > es_prev_sh
        es_lh = es_sh < es_prev_sh

        if (nq_hh and es_lh) or (nq_lh and es_hh):
            confirmed = True
            if nq_hh and es_lh:
                divergence = "bearish"
                criteria = (
                    f"NQ higher high ({nq_sh:.2f}>{nq_prev_sh:.2f}) while ES lower high "
                    f"({es_sh:.2f}<{es_prev_sh:.2f}) — bearish SMR divergence"
                )
            else:
                divergence = "bullish"
                criteria = (
                    f"NQ lower high ({nq_sh:.2f}<{nq_prev_sh:.2f}) while ES higher high "
                    f"({es_sh:.2f}>{es_prev_sh:.2f}) — bullish SMR divergence"
                )
        else:
            criteria = "NQ and ES swing directions aligned — no SMR divergence"

    return SMRResult(
        confirmed=confirmed,
        status=ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED,
        nq_swing_high=nq_sh,
        nq_swing_low=nq_sl,
        es_swing_high=es_sh,
        es_swing_low=es_sl,
        divergence=divergence,
        criteria=criteria,
        nq_candle=nq[-1],
        es_candle=es[-1],
    )



@dataclass
class CISDResult:
    confirmed: bool
    status: ConfirmationStatus
    opposing_candle: Optional[Candle]
    break_candle: Optional[Candle]
    break_level: Optional[float]
    break_price: Optional[float]
    criteria: str


def detect_cisd(
    candles: List[Candle],
    opposing_candle: Candle,
    break_candle: Candle,
) -> CISDResult:
    require_body = CONFIG.cisd.structure_break_require_body_close

    if opposing_candle.is_bullish:
        break_level = opposing_candle.high
    else:
        break_level = opposing_candle.low

    if require_body:
        if break_candle.is_bullish:
            broke = break_candle.close > break_level
        else:
            broke = break_candle.close < break_level
        criteria = (
            f"CISD body-close rule: break_candle.close {break_candle.close:.2f} "
            f"{'>' if break_candle.is_bullish else '<'} break_level {break_level:.2f} -> {broke}"
        )
    else:
        if break_candle.is_bullish:
            broke = break_candle.high > break_level
        else:
            broke = break_candle.low < break_level
        extreme = break_candle.high if break_candle.is_bullish else break_candle.low
        criteria = (
            f"CISD wick rule: break_candle extreme {extreme:.2f} "
            f"{'>' if break_candle.is_bullish else '<'} break_level {break_level:.2f} -> {broke}"
        )

    confirmed = bool(broke)
    return CISDResult(
        confirmed=confirmed,
        status=ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED,
        opposing_candle=opposing_candle,
        break_candle=break_candle,
        break_level=break_level,
        break_price=(
            break_candle.close if require_body else
            (break_candle.high if break_candle.is_bullish else break_candle.low)
        ),
        criteria=criteria,
    )


@dataclass
class FVGResult:
    confirmed: bool
    status: ConfirmationStatus
    fvg_type: Optional[str]
    top: Optional[float]
    bottom: Optional[float]
    mitigation_status: str
    criteria: str
    candle1: Optional[Candle] = None
    candle2: Optional[Candle] = None
    candle3: Optional[Candle] = None


def detect_fvg(candles: List[Candle]) -> FVGResult:
    err = _require(candles, 3, "FVG")
    if err:
        return FVGResult(
            confirmed=False,
            status=ConfirmationStatus.INSUFFICIENT_DATA,
            fvg_type=None,
            top=None,
            bottom=None,
            mitigation_status="unknown",
            criteria=err,
        )

    c1, c2, c3 = _sorted(candles)[-3:]

    fvg_type: Optional[str] = None
    top: Optional[float] = None
    bottom: Optional[float] = None
    criteria = ""

    if c3.low > c1.high:
        fvg_type = "bullish"
        bottom = c1.high
        top = c3.low
        criteria = f"Bullish FVG: c3.low {c3.low:.2f} > c1.high {c1.high:.2f}; gap [{bottom:.2f}, {top:.2f}]"
    elif c3.high < c1.low:
        fvg_type = "bearish"
        top = c1.low
        bottom = c3.high
        criteria = f"Bearish FVG: c3.high {c3.high:.2f} < c1.low {c1.low:.2f}; gap [{bottom:.2f}, {top:.2f}]"
    else:
        criteria = "No three-candle imbalance — no FVG"

    confirmed = fvg_type is not None
    mitigation_status = "unmitigated" if confirmed else "none"

    return FVGResult(
        confirmed=confirmed,
        status=ConfirmationStatus.CONFIRMED if confirmed else ConfirmationStatus.REJECTED,
        fvg_type=fvg_type,
        top=top,
        bottom=bottom,
        mitigation_status=mitigation_status,
        criteria=criteria,
        candle1=c1,
        candle2=c2,
        candle3=c3,
    )


def detect_ifvg(
    candles: List[Candle], prior_fvg: FVGResult
) -> FVGResult:
    result = detect_fvg(candles)
    if result.confirmed:
        result.criteria = f"iFVG (inversion of prior {prior_fvg.fvg_type} FVG): " + result.criteria
    return result

