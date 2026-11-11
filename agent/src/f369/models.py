"""F369 data models.

All messages between agents and the Central Engine are structured dataclasses.
Agents emit *signals*, never trade decisions. The Central Engine owns state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from .config import CONFIG, SESSION_TZ


class Instrument(str, Enum):
    NQ = "NQ"
    ES = "ES"


class Timeframe(str, Enum):
    M1 = "1m"
    M15 = "15m"
    H1 = "1h"
    D1 = "1d"


class Direction(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"


class PatternType(str, Enum):
    LIQUIDITY_SWEEP = "liquidity_sweep"
    SMT = "smt"
    SMR = "smr"
    CISD = "cisd"
    FVG = "fvg"
    iFVG = "ifvg"
    BIAS = "bias"


class ConfirmationStatus(str, Enum):
    CONFIRMED = "confirmed"
    PENDING = "pending"
    INSUFFICIENT_DATA = "insufficient_data"
    REJECTED = "rejected"


class SetupState(str, Enum):
    IDLE = "idle"
    MONITORING = "monitoring"
    ACTIVE_WATCH = "active_watch"
    SMT_CONFIRMED = "smt_confirmed"
    SMR_CONFIRMED = "smr_confirmed"
    CISD_CONFIRMED = "cisd_confirmed"
    ENTRY_READY = "entry_ready"
    ALERT_SENT = "alert_sent"
    INVALIDATED = "invalidated"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candle:
    """A single OHLCV candle with synchronized timestamp."""

    instrument: Instrument
    timeframe: Timeframe
    timestamp: datetime  # UTC
    open: float
    high: float
    low: float
    close: float
    volume: float

    def ny_timestamp(self) -> datetime:
        """Return the candle timestamp in New York session time."""
        return self.timestamp.astimezone(SESSION_TZ)

    @property
    def typical_price(self) -> float:
        return (self.high + self.low + self.close) / 3.0

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open


# ---------------------------------------------------------------------------
# Pattern detection output
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PatternSignal:
    """Deterministic detection result. Every field is populated or the
    signal is marked INSUFFICIENT_DATA — never fabricated."""

    pattern_type: PatternType
    instrument: Instrument
    timeframe: Timeframe
    timestamp: datetime  # UTC
    price_levels: Dict[str, float]  # e.g. {"swing_high": 21050.0, "liq_level": 21050.25}
    detection_criteria: str  # human-readable mathematical rule applied
    confirmation_status: ConfirmationStatus
    nq_candle: Optional[Candle] = None
    es_candle: Optional[Candle] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pattern_type": self.pattern_type.value,
            "instrument": self.instrument.value,
            "timeframe": self.timeframe.value,
            "timestamp": self.timestamp.isoformat(),
            "price_levels": self.price_levels,
            "detection_criteria": self.detection_criteria,
            "confirmation_status": self.confirmation_status.value,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# Directional bias (General Agent output)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DirectionalBias:
    direction: Direction
    timeframe_alignment: Dict[Timeframe, Direction]  # D, 1H, 15m
    key_levels: Dict[str, float]  # HTF liquidity / imbalance levels
    timestamp: datetime
    confidence: float  # 0.0–1.0, deterministic calculation



# ---------------------------------------------------------------------------
# Setup (the state-machine entity owned by Central Engine)
# ---------------------------------------------------------------------------


@dataclass
class Setup:
    """A potential trading setup tracked through the validation state machine."""

    setup_id: str
    instrument: Instrument = Instrument.NQ
    state: SetupState = SetupState.IDLE
    bias: Optional[DirectionalBias] = None
    smt_signal: Optional[PatternSignal] = None
    smr_signal: Optional[PatternSignal] = None
    cisd_signal: Optional[PatternSignal] = None
    fvg_signal: Optional[PatternSignal] = None
    liquidity_sweep: Optional[PatternSignal] = None
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    invalidation_reason: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    def transition(self, new_state: SetupState, reason: str = "") -> None:
        self.state = new_state
        self.updated_at = datetime.utcnow()
        if reason:
            self.notes.append(f"{self.updated_at.isoformat()}: {new_state.value} — {reason}")

    def invalidate(self, reason: str) -> None:
        self.transition(SetupState.INVALIDATED, f"INVALIDATED: {reason}")
        self.invalidation_reason = reason

    def is_terminal(self) -> bool:
        return self.state in {
            SetupState.ALERT_SENT,
            SetupState.INVALIDATED,
            SetupState.COMPLETED,
        }


# ---------------------------------------------------------------------------
# Messages between agents and the Central Engine
# ---------------------------------------------------------------------------


@dataclass
class Message:
    """Envelope for all agent <-> Engine communication."""

    message_id: str
    source_agent: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class BiasEstablished(Message):
    """General Agent -> Central Engine."""

    def __post_init__(self) -> None:
        self.source_agent = "general_agent"


@dataclass
class PatternDetected(Message):
    """Live Agent -> Central Engine."""

    def __post_init__(self) -> None:
        self.source_agent = "live_agent"


@dataclass
class BacktestResult(Message):
    """Backtest Agent -> Central Engine."""

    def __post_init__(self) -> None:
        self.source_agent = "backtest_agent"


@dataclass
class PerformanceSnapshot(Message):
    """Data Analysis Agent -> Central Engine."""

    def __post_init__(self) -> None:
        self.source_agent = "data_analysis_agent"


@dataclass
class RiskVerdict(Message):
    """Mindset Agent -> Central Engine."""

    def __post_init__(self) -> None:
        self.source_agent = "mindset_agent"


@dataclass
class AlertAuthorized(Message):
    """Central Engine -> Alerts Agent."""

    def __post_init__(self) -> None:
        self.source_agent = "central_engine"



# ---------------------------------------------------------------------------
# Shared context — read-only view exposed to agents
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SharedContext:
    """Immutable snapshot of what agents may observe. Agents cannot mutate state."""

    setups: Dict[str, Setup]
    active_bias: Optional[DirectionalBias]
    pattern_history: List[PatternSignal]
    performance_metrics: Dict[str, float]
    risk_limits: RiskConfig
    config: F369Config

    def get_setup(self, setup_id: str) -> Optional[Setup]:
        return self.setups.get(setup_id)

    def active_setups(self) -> List[Setup]:
        return [s for s in self.setups.values() if not s.is_terminal()]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "setup_count": len(self.setups),
            "active_setup_count": len(self.active_setups()),
            "active_bias": (
                {
                    "direction": self.active_bias.direction.value,
                    "timestamp": self.active_bias.timestamp.isoformat(),
                }
                if self.active_bias
                else None
            ),
            "config": {
                "cisd_require_body_close": self.config.strategy.cisd_require_body_close,
                "fvg_require_zone_confirmation": self.config.strategy.fvg_require_zone_confirmation,
            },
        }

