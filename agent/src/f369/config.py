"""
F369 ICT/SMC Configuration

Defines all configuration variables, logic parameters, and dictionaries for the
ICT/SMC algorithmic trading strategy. Handles BOTH bullish and bearish scenarios
explicitly.

Dependency chain enforced by the Central Engine:
    SMT (at Key Liquidity) -> SMR (1m, after SMT) -> CISD (after SMR) -> FVG/iFVG entry

Body Closure vs. Wick Rules:
    - CISD Structure Break: REQUIRES full body candle closure. Wick breaks are INVALID.
    - CISD FVG Retest: Wick OR body retest is VALID.
    - FVG/iFVG Entry Retest: Wick OR body retest is VALID.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time
from enum import Enum
from typing import Any, Dict, FrozenSet, List, Optional
from zoneinfo import ZoneInfo


# ---------------------------------------------------------------------------
# Core Enums
# ---------------------------------------------------------------------------


class Direction(str, Enum):
    """Directional bias — always explicit, never assumed."""
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"


class LiquiditySide(str, Enum):
    """Which side of liquidity a pool represents."""
    BUY_SIDE = "buy_side"      # liquidity above (sell stops / buy-side liquidity)
    SELL_SIDE = "sell_side"    # liquidity below (buy stops / sell-side liquidity)


class Session(str, Enum):
    """Trading sessions used for liquidity pool calculations."""
    ASIA = "asia"
    LONDON = "london"
    NEW_YORK = "new_york"


class LiquidityPoolType(str, Enum):
    """Key liquidity pool types where SMT is evaluated."""
    # Time / Session based
    THIRTY_MIN_HIGH = "thirty_min_high"
    THIRTY_MIN_LOW = "thirty_min_low"
    NINETY_MIN_HIGH = "ninety_min_high"
    NINETY_MIN_LOW = "ninety_min_low"
    LONDON_SESSION_HIGH = "london_session_high"
    LONDON_SESSION_LOW = "london_session_low"
    ASIA_SESSION_HIGH = "asia_session_high"
    ASIA_SESSION_LOW = "asia_session_low"
    # HTF based
    PDH = "previous_daily_high"
    PDL = "previous_daily_low"
    PWH = "previous_weekly_high"
    PWL = "previous_weekly_low"
    PMH = "previous_monthly_high"
    PML = "previous_monthly_low"


class CISDForm(str, Enum):
    """Two valid forms of CISD after SMR confirmation."""
    STRUCTURE_BREAK = "structure_break"
    FVG_RETEST = "fvg_retest"


class SetupState(str, Enum):
    """Setup state machine — enforced sequentially by Central Engine."""
    IDLE = "idle"
    MONITORING = "monitoring"          # Waiting for SMT at Key Liquidity
    ACTIVE_WATCH = "active_watch"      # SMT confirmed at Key Liquidity
    SMR_CONFIRMED = "smr_confirmed"    # 1m SMR confirmed after SMT
    CISD_CONFIRMED = "cisd_confirmed"  # CISD Form 1 or 2 confirmed
    ENTRY_READY = "entry_ready"        # FVG/iFVG entry identified
    ALERT_SENT = "alert_sent"
    INVALIDATED = "invalidated"


class PatternStatus(str, Enum):
    PENDING = "pending"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INSUFFICIENT_DATA = "insufficient_data"



# ---------------------------------------------------------------------------
# Session / Timezone Configuration
# ---------------------------------------------------------------------------


SESSION_TIMEZONE = "America/New_York"
SESSION_TZ = ZoneInfo(SESSION_TIMEZONE)

# Session definitions (New York time)
ASIA_SESSION_START = time(17, 0)   # 5:00 PM NY (prior evening)
ASIA_SESSION_END = time(8, 0)      # 8:00 AM NY
LONDON_SESSION_START = time(2, 0)  # 2:00 AM NY
LONDON_SESSION_END = time(12, 0)   # 12:00 PM NY
NEW_YORK_SESSION_START = time(9, 30)   # 9:30 AM NY
NEW_YORK_SESSION_END = time(16, 0)     # 4:00 PM NY



# ---------------------------------------------------------------------------
# Key Liquidity Pools (Where SMT Occurs)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LiquidityPoolConfig:
    """Configuration for a single key liquidity pool."""

    pool_type: LiquidityPoolType
    side: LiquiditySide
    session: Optional[Session] = None  # None for HTF pools
    lookback_bars: int = 1
    tolerance_ticks: float = 0.25
    align_with_15m_imbalance: bool = False  # 30-min HL must align within 15-min imbalance
    is_active: bool = True


# Registry of all key liquidity pools. SMT is evaluated ONLY when price
# interacts with one of these pools.
KEY_LIQUIDITY_POOLS: Dict[LiquidityPoolType, LiquidityPoolConfig] = {
    # --- Time / Session based ---
    LiquidityPoolType.THIRTY_MIN_HIGH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.THIRTY_MIN_HIGH,
        side=LiquiditySide.BUY_SIDE,
        session=Session.NEW_YORK,
        lookback_bars=1,
        align_with_15m_imbalance=True,
    ),
    LiquidityPoolType.THIRTY_MIN_LOW: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.THIRTY_MIN_LOW,
        side=LiquiditySide.SELL_SIDE,
        session=Session.NEW_YORK,
        lookback_bars=1,
        align_with_15m_imbalance=True,
    ),
    LiquidityPoolType.NINETY_MIN_HIGH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.NINETY_MIN_HIGH,
        side=LiquiditySide.BUY_SIDE,
        session=Session.NEW_YORK,
        lookback_bars=1,
    ),
    LiquidityPoolType.NINETY_MIN_LOW: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.NINETY_MIN_LOW,
        side=LiquiditySide.SELL_SIDE,
        session=Session.NEW_YORK,
        lookback_bars=1,
    ),
    LiquidityPoolType.LONDON_SESSION_HIGH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.LONDON_SESSION_HIGH,
        side=LiquiditySide.BUY_SIDE,
        session=Session.LONDON,
    ),
    LiquidityPoolType.LONDON_SESSION_LOW: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.LONDON_SESSION_LOW,
        side=LiquiditySide.SELL_SIDE,
        session=Session.LONDON,
    ),
    LiquidityPoolType.ASIA_SESSION_HIGH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.ASIA_SESSION_HIGH,
        side=LiquiditySide.BUY_SIDE,
        session=Session.ASIA,
    ),
    LiquidityPoolType.ASIA_SESSION_LOW: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.ASIA_SESSION_LOW,
        side=LiquiditySide.SELL_SIDE,
        session=Session.ASIA,
    ),
    # --- HTF based ---
    LiquidityPoolType.PDH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PDH,
        side=LiquiditySide.BUY_SIDE,
        lookback_bars=1,
    ),
    LiquidityPoolType.PDL: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PDL,
        side=LiquiditySide.SELL_SIDE,
        lookback_bars=1,
    ),
    LiquidityPoolType.PWH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PWH,
        side=LiquiditySide.BUY_SIDE,
        lookback_bars=1,
    ),
    LiquidityPoolType.PWL: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PWL,
        side=LiquiditySide.SELL_SIDE,
        lookback_bars=1,
    ),
    LiquidityPoolType.PMH: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PMH,
        side=LiquiditySide.BUY_SIDE,
        lookback_bars=1,
    ),
    LiquidityPoolType.PML: LiquidityPoolConfig(
        pool_type=LiquidityPoolType.PML,
        side=LiquiditySide.SELL_SIDE,
        lookback_bars=1,
    ),
}

# Set of all sell-side (below) liquidity pools — where bullish SMT can occur.
SELL_SIDE_POOLS: FrozenSet[LiquidityPoolType] = frozenset({
    LiquidityPoolType.THIRTY_MIN_LOW,
    LiquidityPoolType.NINETY_MIN_LOW,
    LiquidityPoolType.LONDON_SESSION_LOW,
    LiquidityPoolType.ASIA_SESSION_LOW,
    LiquidityPoolType.PDL,
    LiquidityPoolType.PWL,
    LiquidityPoolType.PML,
})

# Set of all buy-side (above) liquidity pools — where bearish SMT can occur.
BUY_SIDE_POOLS: FrozenSet[LiquidityPoolType] = frozenset({
    LiquidityPoolType.THIRTY_MIN_HIGH,
    LiquidityPoolType.NINETY_MIN_HIGH,
    LiquidityPoolType.LONDON_SESSION_HIGH,
    LiquidityPoolType.ASIA_SESSION_HIGH,
    LiquidityPoolType.PDH,
    LiquidityPoolType.PWH,
    LiquidityPoolType.PMH,
})



# ---------------------------------------------------------------------------
# SMT Configuration (Smart Money Technique)
# ---------------------------------------------------------------------------
# SMT is evaluated ONLY at KEY_LIQUIDITY_POOLS.
#
# Bullish SMT: at a Key Liquidity Low (sell-side pool). Pair A sweeps the very
#   recent Lower Low (takes out the liquidity), while correlated Pair B fails to
#   sweep and forms a Higher Low (HL).
#
# Bearish SMT: at a Key Liquidity High (buy-side pool). Pair A sweeps the very
#   recent Higher High (takes out the liquidity), while correlated Pair B fails
#   to sweep and forms a Lower High (LH).


@dataclass(frozen=True)
class SMTConfig:
    enabled: bool = True
    required_location: str = "key_liquidity_pool"  # SMT ONLY at KEY_LIQUIDITY_POOLS
    max_sweep_offset_ticks: float = 0.5  # how far past the level counts as a sweep

    # Bullish SMT at Key Liquidity Low
    bullish_require_pair_a_sweep_lower_low: bool = True
    bullish_require_pair_b_fail_sweep: bool = True
    bullish_require_pair_b_higher_low: bool = True

    # Bearish SMT at Key Liquidity High
    bearish_require_pair_a_sweep_higher_high: bool = True
    bearish_require_pair_b_fail_sweep: bool = True
    bearish_require_pair_b_lower_high: bool = True


SMT = SMTConfig()


# ---------------------------------------------------------------------------
# SMR Configuration (Smart Money Reversal)
# ---------------------------------------------------------------------------
# SMR is strictly evaluated on the 1-minute timeframe.
# SMR only becomes valid AFTER an SMT has occurred at Key Liquidity.
# SMR represents the localized 1m structural shift confirming reversal away
# from the SMT liquidity sweep.


@dataclass(frozen=True)
class SMRConfig:
    enabled: bool = True
    timeframe: str = "1m"  # STRICTLY 1-minute — no other timeframe valid
    require_smt_first: bool = True  # SMR invalid without prior SMT at key liquidity
    min_1m_candles_for_structure: int = 3  # minimum 1m candles to establish structure


SMR = SMRConfig()



# ---------------------------------------------------------------------------
# CISD Configuration (Change in State of Delivery)
# ---------------------------------------------------------------------------
# CISD is only valid AFTER SMR is confirmed. Two valid forms:
#
# Form 1 — Structure Break:
#   - REQUIRES a full body candle closure past the structure point.
#   - A wick break is INVALID and must be ignored.
#   - Bullish: full body close ABOVE the 1m swing High that created the SMR's Lower Low.
#   - Bearish: full body close BELOW the 1m swing Low that created the SMR's Higher High.
#
# Form 2 — FVG Retest:
#   - Bullish: bullish 1m FVG forms after SMR; price retraces DOWN into FVG and respects it.
#   - Bearish: bearish 1m FVG forms after SMR; price retraces UP into FVG and respects it.


@dataclass(frozen=True)
class CISDConfig:
    enabled: bool = True
    require_smr_first: bool = True  # CISD invalid without prior SMR confirmation

    # --- Form 1: Structure Break ---
    structure_break_enabled: bool = True
    # CRITICAL: Full body closure required. Wick breaks are INVALID.
    structure_break_require_body_close: bool = True
    structure_break_lookback_bars: int = 10
    structure_break_tolerance_ticks: float = 0.0  # no tolerance for body close

    # --- Form 2: FVG Retest ---
    fvg_retest_enabled: bool = True
    fvg_retest_lookback_bars: int = 20


CISD = CISDConfig()



# ---------------------------------------------------------------------------
# FVG / iFVG Configuration (Fair Value Gap / Inversion FVG)
# ---------------------------------------------------------------------------
# FVG/iFVG retest rule: Unlike the CISD structure break, an FVG or iFVG retest
# does NOT require a body closure. A retest is valid if the zone is hit by
# either a wick OR a candle body.


@dataclass(frozen=True)
class FVGConfig:
    enabled: bool = True
    # Retest valid if zone hit by wick OR body (no body-close requirement)
    retest_allow_wick: bool = True
    retest_allow_body: bool = True
    mitigation_ticks: float = 0.5
    max_retest_bars: int = 10


FVG = FVGConfig()
FVG = FVGConfig()


# ---------------------------------------------------------------------------
# Detection Parameters (mathematical thresholds for pattern detection)
# ---------------------------------------------------------------------------
# These are implementation parameters for the detection engine, not strategy
# rules. Strategy rules (body-vs-wick, opposing structure, entry confirmation)
# live in CISD, SMT, SMR, and FVG configs above.


@dataclass(frozen=True)
class DetectionParams:
    swing_lookback: int = 3  # N candles left/right required for a swing high/low
    swing_tolerance: float = 0.0  # price tolerance for swing equality
    liquidity_buffer_ticks: float = 0.25  # buffer beyond a swing high/low


DETECTION = DetectionParams()


# ---------------------------------------------------------------------------
# Instrument / Pair Configuration
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Instrument / Pair Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstrumentPair:
    primary: str  # e.g., "NQ"
    correlated: str  # e.g., "ES"
    # SMT evaluation: primary takes liquidity, correlated fails (or vice versa)
    primary_sweeps_first: bool = True


DEFAULT_PAIR = InstrumentPair(primary="NQ", correlated="ES", primary_sweeps_first=True)


# ---------------------------------------------------------------------------
# Master Strategy Configuration
# ---------------------------------------------------------------------------


@dataclass
class F369Config:
    """Master configuration. All rule dependencies are explicit."""

    # Sessions / timezone
    session_timezone: str = SESSION_TIMEZONE

    # Key liquidity pools (SMT only here)
    key_liquidity_pools: Dict[LiquidityPoolType, LiquidityPoolConfig] = field(
        default_factory=lambda: KEY_LIQUIDITY_POOLS.copy()
    )
    sell_side_pools: FrozenSet[LiquidityPoolType] = SELL_SIDE_POOLS
    buy_side_pools: FrozenSet[LiquidityPoolType] = BUY_SIDE_POOLS

    # SMT
    smt: SMTConfig = field(default_factory=lambda: SMT)

    # SMR
    smr: SMRConfig = field(default_factory=lambda: SMR)

    # CISD
    cisd: CISDConfig = field(default_factory=lambda: CISD)

    # FVG / iFVG
    fvg: FVGConfig = field(default_factory=lambda: FVG)

    # Detection parameters (mathematical thresholds)
    detection: DetectionParams = field(default_factory=lambda: DETECTION)

    # Instruments
    default_pair: InstrumentPair = DEFAULT_PAIR

    # Runtime flags
    enable_smt: bool = True
    enable_smr: bool = True
    enable_cisd: bool = True
    enable_fvg_entry: bool = True

    # Dependency enforcement (Central Engine reference)
    #   SMT -> SMR -> CISD -> FVG/iFVG entry
    enforce_sequence: bool = True


# Active configuration (mutated at startup once rules are confirmed).
CONFIG = F369Config()

