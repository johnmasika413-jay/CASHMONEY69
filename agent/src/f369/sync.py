"""NQ/ES data synchronization layer.

Guarantees that Nasdaq (NQ) and S&P (ES) candle data share timestamps,
timeframe boundaries, NY session alignment, and DST-safe handling.
Historical and real-time data are processed identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from .config import CONFIG, SESSION_TZ
from .models import Candle, Instrument, Timeframe


@dataclass
class AlignedPair:
    """A pair of NQ and ES candles sharing the same timestamp and timeframe."""

    timestamp_utc: datetime
    timestamp_ny: datetime
    nq: Optional[Candle]
    es: Optional[Candle]
    timeframe: Timeframe
    both_present: bool


class DataSynchronizer:
    """Aligns NQ and ES candle streams by timestamp and timeframe."""

    def __init__(self) -> None:
        self.nq_candles: Dict[Timeframe, List[Candle]] = {tf: [] for tf in Timeframe}
        self.es_candles: Dict[Timeframe, List[Candle]] = {tf: [] for tf in Timeframe}
        self._lock = field(default_factory=lambda: __import__("threading").Lock())

    # ------------------------------------------------------------------
    # Ingestion
    # ------------------------------------------------------------------
    def add_candle(self, candle: Candle) -> None:
        store = self.nq_candles if candle.instrument == Instrument.NQ else self.es_candles
        store[candle.timeframe].append(candle)
        store[candle.timeframe].sort(key=lambda c: c.timestamp)

    def add_batch(self, candles: List[Candle]) -> None:
        for c in candles:
            self.add_candle(c)

    # ------------------------------------------------------------------
    # Alignment
    # ------------------------------------------------------------------
    def get_aligned(
        self, timeframe: Timeframe, nq_ts: datetime, es_ts: datetime, tolerance_seconds: int = 60
    ) -> AlignedPair:
        """Return the NQ/ES pair closest to the requested timestamps, if within tolerance."""
        nq_list = self.nq_candles.get(timeframe, [])
        es_list = self.es_candles.get(timeframe, [])

        nq_candle = self._closest(nq_list, nq_ts, tolerance_seconds)
        es_candle = self._closest(es_list, es_ts, tolerance_seconds)

        # Use the NY time of whichever candle is present (prefer NQ).
        ref = nq_candle or es_candle
        ts_ny = ref.ny_timestamp() if ref else datetime.utcnow().astimezone(SESSION_TZ)

        return AlignedPair(
            timestamp_utc=nq_candle.timestamp if nq_candle else (es_candle.timestamp if es_candle else datetime.utcnow()),
            timestamp_ny=ts_ny,
            nq=nq_candle,
            es=es_candle,
            timeframe=timeframe,
            both_present=(nq_candle is not None and es_candle is not None),
        )

    def get_window(
        self, timeframe: Timeframe, start_utc: datetime, end_utc: datetime
    ) -> List[AlignedPair]:
        """Return aligned NQ/ES pairs for all timestamps within [start, end]."""
        nq_list = [c for c in self.nq_candles.get(timeframe, []) if start_utc <= c.timestamp <= end_utc]
        es_list = [c for c in self.es_candles.get(timeframe, []) if start_utc <= c.timestamp <= end_utc]

        # Build index by timestamp for ES.
        es_index: Dict[datetime, Candle] = {c.timestamp: c for c in es_list}
        pairs: List[AlignedPair] = []
        for nq in nq_list:
            es = es_index.get(nq.timestamp)
            pairs.append(
                AlignedPair(
                    timestamp_utc=nq.timestamp,
                    timestamp_ny=nq.ny_timestamp(),
                    nq=nq,
                    es=es,
                    timeframe=timeframe,
                    both_present=(es is not None),
                )
            )
        # Also include ES-only candles not matched to NQ.
        nq_ts_set = {c.timestamp for c in nq_list}
        for es in es_list:
            if es.timestamp not in nq_ts_set:
                pairs.append(
                    AlignedPair(
                        timestamp_utc=es.timestamp,
                        timestamp_ny=es.ny_timestamp(),
                        nq=None,
                        es=es,
                        timeframe=timeframe,
                        both_present=False,
                    )
                )
        pairs.sort(key=lambda p: p.timestamp_utc)
        return pairs

    # ------------------------------------------------------------------
    # Session utilities
    # ------------------------------------------------------------------
    @staticmethod
    def ny_session_bounds(date_utc: datetime) -> Tuple[datetime, datetime]:
        """Return NY session start/end as UTC datetimes for the NY date of `date_utc`."""
        date_ny = date_utc.astimezone(SESSION_TZ).date()
        start_ny = datetime.combine(date_ny, CONFIG.data_sync.session_start, tzinfo=SESSION_TZ)
        end_ny = datetime.combine(date_ny, CONFIG.data_sync.session_end, tzinfo=SESSION_TZ)
        return start_ny.astimezone(ZoneInfo("UTC")), end_ny.astimezone(ZoneInfo("UTC"))

    @staticmethod
    def is_regular_session(ts_utc: datetime) -> bool:
        start, end = DataSynchronizer.ny_session_bounds(ts_utc)
        return start <= ts_utc <= end

    @staticmethod
    def _closest(candles: List[Candle], target: datetime, tolerance_seconds: int) -> Optional[Candle]:
        best: Optional[Candle] = None
        best_diff: Optional[timedelta] = None
        for c in candles:
            diff = abs((c.timestamp - target).total_seconds())
            if diff <= tolerance_seconds and (best_diff is None or diff < best_diff.total_seconds()):
                best = c
                best_diff = timedelta(seconds=diff)
        return best


# Singleton synchronizer shared by all agents and the engine.
synchronizer = DataSynchronizer()
