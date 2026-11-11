"""General Agent — Orderflow General Agent for NQ (Nasdaq-100 Futures).

Two layers live in this module:

1. ``OrderflowGeneralAgent`` — deterministic ICT/SMT orderflow engine for
   high-frequency bias determination across Monthly, Weekly, Daily (1D),
   Hourly (1H), and 15m/30m/90m timeframes with strict top-down dominance:
       1. Daily (1D) — Highest Priority (dictates Monthly/Weekly trajectory & primary draw)
       2. Hourly (1H) — Secondary Priority (session dependency chain & HOD/LOD SMT anchors)
       3. 15m/30m/90m — Tertiary Priority (time cycle retracements & imbalance execution)
2. ``GeneralAgent`` — lightweight BaseAgent adapter that establishes the daily
   directional bias using D, 1H, and 15m candles and emits a BiasEstablished
   signal to the Central Engine. Does not make trade decisions.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple

# Attempt to import established parameters from config.py with safe fallbacks
try:
    import config
except ImportError:
    class _FallbackConfig:
        PRIMARY_SYMBOL = "NQ"
        BENCHMARK_SYMBOL = "ES"
        TIMEFRAME_WEIGHTS = {
            "1D": 0.60,   # Dominant weight: dictates where price is going in the month/week
            "1H": 0.25,   # Intermediate weight: governs intraday session HOD/LOD structure
            "15m": 0.15,  # Execution weight: refines entries via 30m/90m cycles & FVGs
        }
        REVERSAL_SEQUENCE = ("SMT_DIVERGENCE", "SMR", "CISD")
        SESSIONS = {
            "ASIA": ("18:00", "00:00"),
            "LONDON": ("00:00", "06:00"),
            "NY_AM": ("06:00", "12:00"),
        }
        MACRO_CYCLES_MINUTES = (90, 30)
        EQUILIBRIUM_LEVEL = 0.50
        EQUILIBRIUM_TOLERANCE = 0.03
        IMBALANCE_TYPES = ("FVG", "VI")
        HTF_LEVELS = ("PMH", "PML", "PWH", "PWL", "PDH", "PDL")

    config = _FallbackConfig()  # type: ignore

logger = logging.getLogger(__name__)

from ..bus import MessageBus
from ..engine import engine
from ..models import Candle, Direction, Instrument, Timeframe
from ..sync import synchronizer
from .base import BaseAgent


# ============================================================================
# ORDERFLOW GENERAL AGENT — ENUMS & DATA STRUCTURES
# ============================================================================

class DirectionalTrajectory(str, Enum):
    CONTINUATION_BULLISH = "Bullish Expansion / Continuation"
    CONTINUATION_BEARISH = "Bearish Expansion / Continuation"
    REVERSAL_BULLISH = "Bullish Reversal"
    REVERSAL_BEARISH = "Bearish Reversal"
    NEUTRAL_CONSOLIDATION = "Neutral / Awaiting Liquidity Sweep"


class SessionAnchorScenario(str, Enum):
    SCENARIO_A_ASIA = "Scenario A (Asia SMT Anchor)"
    SCENARIO_B_LONDON = "Scenario B (London SMT Anchor)"
    SCENARIO_C_NY_AM = "Scenario C (NY AM SMT Anchor)"
    UNRESOLVED = "Unresolved (No Valid Session SMT Anchor Locked)"


class FinalBias(str, Enum):
    STRONGLY_BULLISH = "Strongly Bullish"
    STRONGLY_BEARISH = "Strongly Bearish"


@dataclass
class ReversalSequenceState:
    """Tracks the mandatory 3-step reversal sequence: SMT -> SMR -> CISD."""
    smt_divergence: bool = False
    smr_confirmed: bool = False
    cisd_confirmed: bool = False
    swept_by_nq_only: Optional[bool] = None
    details: str = ""

    @property
    def is_valid_reversal(self) -> bool:
        """A reversal is ONLY valid if all 3 steps occur in strict sequence."""
        return bool(self.smt_divergence and self.smr_confirmed and self.cisd_confirmed)

    def format_chain(self) -> str:
        smt_str = "Confirmed (Single-Pair Sweep)" if self.smt_divergence else "Absent (Correlated Sweep / None)"
        smr_str = "Confirmed" if self.smr_confirmed else "Pending/Failed"
        cisd_str = "Confirmed" if self.cisd_confirmed else "Pending/Failed"
        return f"[SMT: {smt_str}] -> [SMR: {smr_str}] -> [CISD: {cisd_str}]"

@dataclass
class LevelSweepEvent:
    """Represents interaction with a key HTF or Session liquidity level."""
    level_name: str               # e.g., "PWL", "PMH", "PDH", "PDL", "ASIA_H", "LONDON_L"
    direction: str                # "HIGH" or "LOW"
    price: float
    nq_swept: bool
    es_swept: bool
    session: str                  # "ASIA", "LONDON", "NY_AM", or "HTF"
    smr_confirmed: bool = False
    cisd_confirmed: bool = False

    @property
    def has_intermarket_smt(self) -> bool:
        """SMT occurs when NQ sweeps a level and ES fails to sweep, or vice versa."""
        return self.nq_swept != self.es_swept

    @property
    def nq_led_smt(self) -> bool:
        """Specific trigger where NQ sweeps the level and ES does not."""
        return self.nq_swept and not self.es_swept

    @property
    def both_swept_no_smt(self) -> bool:
        """Correlated sweep by both NQ and ES indicating continuation strength."""
        return self.nq_swept and self.es_swept

    def to_reversal_state(self) -> ReversalSequenceState:
        return ReversalSequenceState(
            smt_divergence=self.has_intermarket_smt,
            smr_confirmed=self.smr_confirmed if self.has_intermarket_smt else False,
            cisd_confirmed=(
                self.cisd_confirmed
                if (self.has_intermarket_smt and self.smr_confirmed)
                else False
            ),
            swept_by_nq_only=self.nq_led_smt,
            details=f"Level {self.level_name} ({self.price}) in {self.session}",
        )


@dataclass
class ImbalanceZone:
    """Represents a Fair Value Gap (FVG) or Volume Imbalance (VI)."""
    imbalance_type: str           # "FVG" or "VI"
    timeframe: str                # "1D", "1H", "30m", "15m"
    direction: str                # "BULLISH" (Discount) or "BEARISH" (Premium)
    upper_bound: float
    lower_bound: float
    origin_session: str           # "ASIA", "LONDON", "NY_AM", "HTF"
    swept_old_hl: bool = False    # True if formation swept an old High/Low
    dealing_range_pct: float = 0.0  # Location in active dealing range (0.0 to 1.0)

    @property
    def midpoint(self) -> float:
        return (self.upper_bound + self.lower_bound) / 2.0

    def is_at_equilibrium(self, tolerance: float = 0.03) -> bool:
        eq = getattr(config, "EQUILIBRIUM_LEVEL", 0.50)
        return abs(self.dealing_range_pct - eq) <= tolerance

    def is_validated_30m_anchor(self) -> bool:
        """
        Validates an imbalance when session SMT is absent:
        MUST have swept an old high/low OR formed at the 50% Equilibrium level.
        """
        tol = getattr(config, "EQUILIBRIUM_TOLERANCE", 0.03)
        return self.swept_old_hl or self.is_at_equilibrium(tolerance=tol)

@dataclass
class TimeCycleEvent:
    """Represents a 90m or 30m macro cycle high/low interaction."""
    cycle_minutes: int            # 90 or 30
    extremum_type: str            # "LOW" or "HIGH"
    price: float
    timestamp: str
    inside_imbalance: Optional[ImbalanceZone] = None
    local_smt_present: bool = False


@dataclass
class MarketStatePayload:
    """Complete multi-timeframe market snapshot passed to the General Agent."""
    current_price: float
    monthly_weekly_context: Dict[str, float]  # PMH, PML, PWH, PWL
    previous_day_context: Dict[str, Any]      # PDH, PDL, previous_day_delivery ("BULLISH_EXPANSION", etc.)
    htf_sweeps: List[LevelSweepEvent] = field(default_factory=list)
    session_sweeps: List[LevelSweepEvent] = field(default_factory=list)
    imbalances: List[ImbalanceZone] = field(default_factory=list)
    time_cycles: List[TimeCycleEvent] = field(default_factory=list)
    daily_trend_bias: Optional[str] = None    # "BULLISH" or "BEARISH" from Daily structure

# ============================================================================
# SYSTEM PROMPT (STRICT ICT/SMT GENERAL AGENT INSTRUCTIONS)
# ============================================================================

GENERAL_AGENT_SYSTEM_PROMPT = """You are the Orderflow General Agent for NQ (Nasdaq-100 Futures).
Your sole mandate is high-frequency institutional bias determination across Monthly, Weekly, Daily (1D), Hourly (1H), and 15m/30m/90m timeframes using strict ICT/SMT Orderflow Laws.

### TIMEFRAME HIERARCHY & DOMINANCE LAW (CRITICAL)
1. **Daily Chart (1D) — SUPREME AUTHORITY (Weight: 60%)**:
   - The Daily chart is the highest analytical anchor and carries the strongest directional authority.
   - It determines where price is going across the current Week and Month (towards PMH/PML or PWH/PWL and Daily FVGs/VIs).
   - Lower timeframes (1H and 15m/30m) are strictly subordinate to the Daily bias. Intraday counter-trend moves against the Daily chart must be treated as temporary retracements into Daily Premium/Discount imbalances unless a full HTF 3-Step Reversal Sequence has completed at a Weekly/Monthly/Daily extreme.
2. **Hourly Chart (1H) — SESSION DEPENDENCY ANCHOR (Weight: 25%)**:
   - Bridges the Daily draw on liquidity with intraday execution by establishing the High of the Day (HOD) or Low of the Day (LOD) via PDH/PDL and Asia -> London -> NY AM session sweeps.
3. **15m / 30m / 90m Charts — EXECUTION & TIME CYCLES (Weight: 15%)**:
   - Isolates precision entries during NY Morning using 90m/30m cycles retesting validated session imbalances in the direction of the Daily/Hourly bias.

---

### CORE STRUCTURAL LAW (Macro Expectations)
- **Previous Candle Principle**: Previous Month (PM), Previous Week (PW), and Previous Day (PD) highs and lows define the active liquidity boundaries.
- **PXL/H Sweep Dynamics**: When a Previous High or Low (PMH, PML, PWH, PWL, PDH, PDL) is swept, price can ONLY:
  1. **Expand / Continue**: Drive further beyond the swept level.
  2. **Reverse**: Fail to continue and reverse toward opposing liquidity.
- **Mandatory 3-Step Reversal Sequence**: A reversal is INVALID unless all three steps occur in strict chronological order:
  1. **SMT Divergence**: Intermarket failure between NQ and ES at the liquidity sweep (e.g., NQ sweeps the level while ES fails to sweep).
  2. **SMR**: Smart Money Reversal price action confirmation at the sweep.
  3. **CISD**: Change in State of Delivery (Change in Supply/Demand confirmation).

---

### STEP-BY-STEP ORDERFLOW ENGINE LOGIC

#### 1. Daily / High Timeframe (HTF) Bias Engine (Highest Priority)
- **Bullish Bias Trigger**: PWL, PML, or PDL (within HTF discount) is swept on NQ and NOT by ES (Intermarket SMT), followed sequentially by SMR and CISD confirmation.
  - *Primary Targets*: Premium Daily imbalances (FVG/VI), PMH, or PWH.
- **Bearish Bias Trigger**: PWH, PMH, or PDH (within HTF premium) is swept on NQ and NOT by ES (Intermarket SMT), followed sequentially by SMR and CISD confirmation.
  - *Primary Targets*: Discount Daily imbalances (FVG/VI), PML, or PWL.
- **Continuation Condition**: If PXL/H is swept WITH ES (No SMT) and price shows no SMR/CISD, mandate expansion/continuation higher or lower into the next HTF liquidity pool or Daily imbalance.


#### 2. Hourly (1H) Timeframe Context (Session Dependency Law)
- **Previous Day Principle**: How the previous day delivered dictates the current day's structural framework. PDH and PDL are the paramount intraday anchors.
- **Intraday Dependency Chain**: Asia sets initial boundaries -> London reacts to Asia/HTF levels -> NY Morning reacts to London/Asia/HTF levels.
- **LOD / HOD Formation (The SMT Anchor Scenarios)**:
  - **Scenario A (Asia SMT Anchor)**: Price sweeps an HTF level (PDH/PDL/PWL/PWH) in Asia with SMT. The true LOD/HOD is locked early. London and NY AM expand away from this Asia anchor.
  - **Scenario B (London SMT Anchor)**: Price sweeps the Asia High/Low or an HTF level during London with SMT. NY Morning either expands directly away from this London SMT or engineers a retracement into a London-created structural imbalance before continuing expansion in alignment with Daily bias.
  - **Scenario C (NY AM SMT Anchor)**: London remains inside the Asia range or builds liquidity. NY Morning forcefully sweeps the London High/Low, Asia High/Low, or remaining HTF level with SMT (near 9:30 AM EST open or macro news) to lock the true LOD/HOD and reverse the intraday trend.

#### 3. 15m / 30m / 90m Execution & Time Cycle Rules
- **Time Cycle Setups (90m / 30m)**: During NY Morning, track 90m and 30m macro cycles for retracements into key imbalances (FVG/VI) explicitly created by the session that locked in the LOD/HOD.
- **The Cycle Execution Trigger**: High-probability execution is confirmed when a 90m or 30m cycle low/high forms inside a validated session imbalance AND is accompanied by local SMT at that exact time/price intersection.
- **Alternative (No Session SMT)**: If NO SMT is present at major session liquidity levels:
  1. Identify a 30-Minute Low/High formed inside a key imbalance (FVG/VI).
  2. Validate the imbalance: It MUST have either swept an old high/low OR formed at the 50% Equilibrium (EQ) level of the active dealing range.
  3. Use this 30m validated level as the key structural anchor for session direction.

---

### STRICT OUTPUT FORMAT
You MUST output your analysis using the exact four-section structure below without omitting any field:

Macro Context & Reversal Sequence (HTF):
- Dominant Daily (1D) Authority & Monthly/Weekly Draw: [Analysis of 1D priority & monthly/weekly trajectory]
- Key PXL/H Level Swept: [PMH / PML / PWH / PWL / PDH / PDL or None]
- Reversal Verification: [SMT Status] -> [SMR Status] -> [CISD Status]
- Directional Trajectory: [Continuation vs. Reversal]

Session Dependency & Liquidity Sweep Status (1H):
- Previous Day Context: [Previous day delivery & PDH/PDL interaction]
- Active LOD/HOD Scenario: [Scenario A (Asia SMT Anchor) | Scenario B (London SMT Anchor) | Scenario C (NY AM SMT Anchor) | Unresolved]

Time Cycle Execution & Imbalance Validation (15m/30m/90m):
- 90m/30m Cycle Retracement Status: [Analysis of cycle retracements into LOD/HOD session FVGs/VIs + local SMT]
- 30m Low/High Validation Point: [Equilibrium 50% or Old H/L Sweep validation details]

Final Session Bias & Core Targets:
- Directional Bias: [Strongly Bullish | Strongly Bearish] (Weighted: 1D > 1H > 15m/30m)
- Primary Liquidity & Imbalance Targets: [Ordered list of Daily/Weekly/Monthly targets & intraday pools]
"""

# ============================================================================
# ORDERFLOW GENERAL AGENT ENGINE
# ============================================================================

class OrderflowGeneralAgent:
    """
    Orderflow General Agent for NQ Futures.
    Executes deterministic multi-timeframe ICT/SMT rules with Daily > 1H > 15m priority
    and interfaces with an LLM client using the strict system prompt when provided.
    """

    def __init__(self, llm_client: Optional[Callable[[str, str], str]] = None) -> None:
        """
        Args:
            llm_client: Optional callable taking (system_prompt, user_prompt) and returning
                        the LLM completion string. If None, uses the deterministic rule engine
                        to generate the exact structured payload.
        """
        self.llm_client = llm_client
        self.system_prompt = GENERAL_AGENT_SYSTEM_PROMPT
        self.weights = getattr(
            config,
            "TIMEFRAME_WEIGHTS",
            {"1D": 0.60, "1H": 0.25, "15m": 0.15},
        )

    # ------------------------------------------------------------------------
    # 1. DAILY / HTF BIAS ENGINE (HIGHEST PRIORITY)
    # ------------------------------------------------------------------------

    def evaluate_htf_bias(self, state: MarketStatePayload) -> Dict[str, Any]:
        """
        Evaluates Daily/Weekly/Monthly sweeps, the 3-step reversal sequence,
        and establishes the supreme Daily (1D) bias and primary draw on liquidity.
        """
        htf_levels = getattr(config, "HTF_LEVELS", ("PMH", "PML", "PWH", "PWL", "PDH", "PDL"))
        relevant_sweeps = [s for s in state.htf_sweeps if s.level_name in htf_levels]

        daily_imbalances = [imb for imb in state.imbalances if imb.timeframe == "1D"]
        premium_daily_imbalances = [
            f"1D {i.imbalance_type} ({i.lower_bound:.2f}-{i.upper_bound:.2f})"
            for i in daily_imbalances if i.direction == "BEARISH"
        ]
        discount_daily_imbalances = [
            f"1D {i.imbalance_type} ({i.lower_bound:.2f}-{i.upper_bound:.2f})"
            for i in daily_imbalances if i.direction == "BULLISH"
        ]

        pmh = state.monthly_weekly_context.get("PMH")
        pml = state.monthly_weekly_context.get("PML")
        pwh = state.monthly_weekly_context.get("PWH")
        pwl = state.monthly_weekly_context.get("PWL")

        if not relevant_sweeps:
            # Fall back to dominant Daily structural trend when no immediate HTF sweep occurred
            is_bullish = (state.daily_trend_bias or "BULLISH").upper() == "BULLISH"
            trajectory = (
                DirectionalTrajectory.CONTINUATION_BULLISH
                if is_bullish
                else DirectionalTrajectory.CONTINUATION_BEARISH
            )
            targets = (
                premium_daily_imbalances + [f"PWH ({pwh})", f"PMH ({pmh})"]
                if is_bullish
                else discount_daily_imbalances + [f"PWL ({pwl})", f"PML ({pml})"]
            )
            return {
                "dominant_1d_bias": "BULLISH" if is_bullish else "BEARISH",
                "swept_level": "None (Operating inside active Daily expansion range)",
                "reversal_state": ReversalSequenceState(),
                "trajectory": trajectory,
                "primary_targets": targets,
            }

        # Evaluate most recent macro sweep
        latest_sweep = relevant_sweeps[-1]
        rev_state = latest_sweep.to_reversal_state()

        # Case 1: Low Swept (PWL / PML / PDL)
        if latest_sweep.direction == "LOW":
            if latest_sweep.nq_led_smt and rev_state.is_valid_reversal:
                # Bullish Reversal Trigger
                targets = premium_daily_imbalances + [f"PWH ({pwh})", f"PMH ({pmh})"]
                return {
                    "dominant_1d_bias": "BULLISH",
                    "swept_level": f"{latest_sweep.level_name} @ {latest_sweep.price:.2f} (NQ swept, ES held)",
                    "reversal_state": rev_state,
                    "trajectory": DirectionalTrajectory.REVERSAL_BULLISH,
                    "primary_targets": targets,
                }
            elif latest_sweep.both_swept_no_smt and not (latest_sweep.smr_confirmed or latest_sweep.cisd_confirmed):
                # Bearish Expansion / Continuation
                targets = discount_daily_imbalances + [f"PWL ({pwl})", f"PML ({pml})"]
                return {
                    "dominant_1d_bias": "BEARISH",
                    "swept_level": f"{latest_sweep.level_name} @ {latest_sweep.price:.2f} (Swept with ES - No SMT)",
                    "reversal_state": rev_state,
                    "trajectory": DirectionalTrajectory.CONTINUATION_BEARISH,
                    "primary_targets": targets,
                }

        # Case 2: High Swept (PWH / PMH / PDH)
        if latest_sweep.direction == "HIGH":
            if latest_sweep.nq_led_smt and rev_state.is_valid_reversal:
                # Bearish Reversal Trigger
                targets = discount_daily_imbalances + [f"PWL ({pwl})", f"PML ({pml})"]
                return {
                    "dominant_1d_bias": "BEARISH",
                    "swept_level": f"{latest_sweep.level_name} @ {latest_sweep.price:.2f} (NQ swept, ES held)",
                    "reversal_state": rev_state,
                    "trajectory": DirectionalTrajectory.REVERSAL_BEARISH,
                    "primary_targets": targets,
                }
            elif latest_sweep.both_swept_no_smt and not (latest_sweep.smr_confirmed or latest_sweep.cisd_confirmed):
                # Bullish Expansion / Continuation
                targets = premium_daily_imbalances + [f"PWH ({pwh})", f"PMH ({pmh})"]
                return {
                    "dominant_1d_bias": "BULLISH",
                    "swept_level": f"{latest_sweep.level_name} @ {latest_sweep.price:.2f} (Swept with ES - No SMT)",
                    "reversal_state": rev_state,
                    "trajectory": DirectionalTrajectory.CONTINUATION_BULLISH,
                    "primary_targets": targets,
                }

        # Incomplete reversal sequence defaults back to Daily structural trend priority
        fallback_bullish = (state.daily_trend_bias or "BULLISH").upper() == "BULLISH"
        return {
            "dominant_1d_bias": "BULLISH" if fallback_bullish else "BEARISH",
            "swept_level": f"{latest_sweep.level_name} @ {latest_sweep.price:.2f} (Incomplete Reversal Sequence)",
            "reversal_state": rev_state,
            "trajectory": (
                DirectionalTrajectory.CONTINUATION_BULLISH
                if fallback_bullish
                else DirectionalTrajectory.CONTINUATION_BEARISH
            ),
            "primary_targets": (
                premium_daily_imbalances + [f"PWH ({pwh})", f"PMH ({pmh})"]
                if fallback_bullish
                else discount_daily_imbalances + [f"PWL ({pwl})", f"PML ({pml})"]
            ),
        }

    # ------------------------------------------------------------------------
    # 2. HOURLY (1H) SESSION DEPENDENCY & HOD/LOD ENGINE (SECOND PRIORITY)
    # ------------------------------------------------------------------------

    def evaluate_hourly_session_context(self, state: MarketStatePayload) -> Dict[str, Any]:
        """
        Evaluates PDH/PDL interaction and the Asia -> London -> NY AM dependency chain
        to isolate Scenario A, Scenario B, or Scenario C for HOD/LOD formation.
        """
        pdh = state.previous_day_context.get("PDH")
        pdl = state.previous_day_context.get("PDL")
        pd_delivery = state.previous_day_context.get("previous_day_delivery", "STANDARD_DELIVERY")

        pd_interactions = [
            s for s in (state.htf_sweeps + state.session_sweeps)
            if s.level_name in ("PDH", "PDL")
        ]
        if pd_interactions:
            last_pd = pd_interactions[-1]
            pd_status = (
                f"Prior Day Delivery: {pd_delivery} | {last_pd.level_name} ({last_pd.price:.2f}) "
                f"swept in {last_pd.session} (SMT: {last_pd.has_intermarket_smt})"
            )
        else:
            pd_status = (
                f"Prior Day Delivery: {pd_delivery} | Price trading inside PDH ({pdh}) / PDL ({pdl}) boundaries"
            )

        # Evaluate SMT Anchor Scenarios in chronological order (Asia -> London -> NY_AM)
        scenario = SessionAnchorScenario.UNRESOLVED
        anchor_session = None
        anchor_direction = None  # "BULLISH" (LOD locked) or "BEARISH" (HOD locked)
        scenario_details = "No session SMT sweep has locked the HOD/LOD yet."

        for sweep in state.session_sweeps:
            if not sweep.has_intermarket_smt:
                continue

            is_lod_locked = sweep.direction == "LOW"
            derived_dir = "BULLISH" if is_lod_locked else "BEARISH"
            extremum_label = "LOD" if is_lod_locked else "HOD"

            if sweep.session == "ASIA" and sweep.level_name in ("PDH", "PDL", "PWH", "PWL", "PMH", "PML"):
                scenario = SessionAnchorScenario.SCENARIO_A_ASIA
                anchor_session = "ASIA"
                anchor_direction = derived_dir
                scenario_details = (
                    f"{scenario.value}: Asia swept {sweep.level_name} ({sweep.price:.2f}) with SMT. "
                    f"True {extremum_label} locked early; London and NY AM set for {derived_dir.lower()} expansion away from Asia anchor."
                )

            elif sweep.session == "LONDON" and (
                sweep.level_name.startswith("ASIA") or sweep.level_name in ("PDH", "PDL", "PWH", "PWL")
            ):
                scenario = SessionAnchorScenario.SCENARIO_B_LONDON
                anchor_session = "LONDON"
                anchor_direction = derived_dir
                scenario_details = (
                    f"{scenario.value}: London swept {sweep.level_name} ({sweep.price:.2f}) with SMT to lock {extremum_label}. "
                    f"NY Morning primed to expand directly or retrace into a London structural imbalance before {derived_dir.lower()} continuation."
                )

            elif sweep.session == "NY_AM" and (
                sweep.level_name.startswith("LONDON")
                or sweep.level_name.startswith("ASIA")
                or sweep.level_name in ("PDH", "PDL", "PWH", "PWL")
            ):
                scenario = SessionAnchorScenario.SCENARIO_C_NY_AM
                anchor_session = "NY_AM"
                anchor_direction = derived_dir
                scenario_details = (
                    f"{scenario.value}: NY Morning swept {sweep.level_name} ({sweep.price:.2f}) with SMT to lock true {extremum_label}, "
                    f"reversing intraday delivery into {derived_dir.lower()} expansion."
                )

        return {
            "previous_day_status": pd_status,
            "scenario": scenario,
            "anchor_session": anchor_session,
            "anchor_direction": anchor_direction,
            "scenario_details": scenario_details,
        }

    # ------------------------------------------------------------------------
    # 3. 15M / 30M / 90M EXECUTION & TIME CYCLE ENGINE (THIRD PRIORITY)
    # ------------------------------------------------------------------------

    def evaluate_time_cycles_and_imbalances(
        self,
        state: MarketStatePayload,
        anchor_session: Optional[str],
        dominant_1d_bias: str,
    ) -> Dict[str, Any]:
        """
        Evaluates 90m/30m NY AM macro cycle retracements into imbalances created by
        the HOD/LOD session, or applies the Alternative 30m Validation Rule if no SMT exists.
        """
        cycle_confirmations: List[str] = []
        validated_30m_anchors: List[str] = []
        execution_bias: Optional[str] = None

        # Check 90m and 30m cycle retracements into session imbalances
        for cycle in state.time_cycles:
            if cycle.inside_imbalance is not None:
                imb = cycle.inside_imbalance
                matches_origin = (anchor_session is None) or (imb.origin_session == anchor_session)
                if matches_origin and cycle.local_smt_present:
                    derived_dir = "BULLISH" if cycle.extremum_type == "LOW" else "BEARISH"
                    execution_bias = derived_dir
                    cycle_confirmations.append(
                        f"{cycle.cycle_minutes}m Cycle {cycle.extremum_type} @ {cycle.price:.2f} ({cycle.timestamp}) "
                        f"formed inside {imb.origin_session} {imb.timeframe} {imb.imbalance_type} "
                        f"({imb.lower_bound:.2f}-{imb.upper_bound:.2f}) WITH Local SMT [HIGH-PROBABILITY TRIGGER]"
                    )

        # Alternative Rule (when no session SMT is locked or as structural 30m anchor check)
        for imb in state.imbalances:
            if imb.timeframe in ("30m", "15m") and imb.is_validated_30m_anchor():
                reasons = []
                if imb.swept_old_hl:
                    reasons.append("Swept Old High/Low")
                if imb.is_at_equilibrium():
                    reasons.append(f"Formed at 50% Equilibrium ({imb.dealing_range_pct * 100:.1f}%)")
                reason_str = " & ".join(reasons)
                validated_30m_anchors.append(
                    f"Validated {imb.timeframe} {imb.direction} {imb.imbalance_type} "
                    f"({imb.lower_bound:.2f}-{imb.upper_bound:.2f}) — {reason_str}"
                )
                if execution_bias is None and imb.direction == dominant_1d_bias:
                    execution_bias = imb.direction

        cycle_summary = (
            "; ".join(cycle_confirmations)
            if cycle_confirmations
            else "No 90m/30m cycle with local SMT inside HOD/LOD session imbalance; relying on 30m structural validation."
        )
        validation_summary = (
            "; ".join(validated_30m_anchors)
            if validated_30m_anchors
            else "Awaiting 30m High/Low formation inside an Equilibrium (50%) or Liquidity-Sweeping FVG/VI."
        )

        return {
            "cycle_summary": cycle_summary,
            "validation_summary": validation_summary,
            "execution_bias": execution_bias or dominant_1d_bias,
        }

    # ------------------------------------------------------------------------
    # 4. HIERARCHICAL BIAS SYNTHESIS (1D > 1H > 15M/30M)
    # ------------------------------------------------------------------------

    def synthesize_final_bias(
        self,
        dominant_1d_bias: str,
        hourly_bias: Optional[str],
        execution_bias: Optional[str],
    ) -> Tuple[FinalBias, str]:
        """
        Synthesizes final directional bias enforcing strict priority:
        Daily (1D) > Hourly (1H) > 15m/30m.
        Because 1D carries 60% weight (greater than 1H + 15m combined), the Daily
        chart dictates the primary monthly/weekly trajectory.
        """
        w_1d = self.weights.get("1D", 0.60)
        w_1h = self.weights.get("1H", 0.25)
        w_15m = self.weights.get("15m", 0.15)

        score = 0.0
        score += w_1d if dominant_1d_bias == "BULLISH" else -w_1d
        if hourly_bias:
            score += w_1h if hourly_bias == "BULLISH" else -w_1h
        else:
            score += w_1h if dominant_1d_bias == "BULLISH" else -w_1h

        if execution_bias:
            score += w_15m if execution_bias == "BULLISH" else -w_15m
        else:
            score += w_15m if dominant_1d_bias == "BULLISH" else -w_15m

        final_bias = FinalBias.STRONGLY_BULLISH if score >= 0 else FinalBias.STRONGLY_BEARISH

        alignment_note = (
            f"1D ({dominant_1d_bias}, weight={w_1d:.2f}) > "
            f"1H ({hourly_bias or 'Aligned w/ 1D'}, weight={w_1h:.2f}) > "
            f"15m/30m ({execution_bias or 'Aligned w/ 1D'}, weight={w_15m:.2f})"
        )
        return final_bias, alignment_note

    # ------------------------------------------------------------------------
    # 5. MAIN EXECUTION & FORMATTED OUTPUT GENERATOR
    # ------------------------------------------------------------------------

    def analyze(self, state: MarketStatePayload, use_llm: bool = False) -> str:
        """
        Runs the complete 3-tier Orderflow Engine and returns the analysis formatted
        strictly according to the required output specification.
        """
        htf_res = self.evaluate_htf_bias(state)
        hourly_res = self.evaluate_hourly_session_context(state)
        cycle_res = self.evaluate_time_cycles_and_imbalances(
            state=state,
            anchor_session=hourly_res["anchor_session"],
            dominant_1d_bias=htf_res["dominant_1d_bias"],
        )

        final_bias, alignment_note = self.synthesize_final_bias(
            dominant_1d_bias=htf_res["dominant_1d_bias"],
            hourly_bias=hourly_res["anchor_direction"],
            execution_bias=cycle_res["execution_bias"],
        )

        deterministic_report = self._format_output_payload(
            htf_res=htf_res,
            hourly_res=hourly_res,
            cycle_res=cycle_res,
            final_bias=final_bias,
            alignment_note=alignment_note,
        )

        if use_llm and self.llm_client is not None:
            user_prompt = (
                "Analyze the following NQ & ES multi-timeframe orderflow telemetry and pre-computed "
                "structural checks. Output strictly in the required 4-section format, preserving the "
                "Daily (1D) > Hourly (1H) > 15m/30m priority hierarchy.\n\n"
                f"RAW TELEMETRY:\n{json.dumps(asdict(state), indent=2, default=str)}\n\n"
                f"ENGINE BASELINE:\n{deterministic_report}"
            )
            return self.llm_client(self.system_prompt, user_prompt)

        return deterministic_report

    @staticmethod
    def _format_output_payload(
        htf_res: Dict[str, Any],
        hourly_res: Dict[str, Any],
        cycle_res: Dict[str, Any],
        final_bias: FinalBias,
        alignment_note: str,
    ) -> str:
        """Formats the engine output to match the exact required payload schema."""
        rev_chain = htf_res["reversal_state"].format_chain()
        targets_str = ", ".join(htf_res["primary_targets"])

        return (
            "Macro Context & Reversal Sequence (HTF):\n"
            f"- Dominant Daily (1D) Authority & Monthly/Weekly Draw: Daily chart is {htf_res['dominant_1d_bias']} "
            f"(Supreme Priority — dictates Monthly/Weekly expansion path).\n"
            f"- Key PXL/H Level Swept: {htf_res['swept_level']}\n"
            f"- Reversal Verification: {rev_chain}\n"
            f"- Directional Trajectory: {htf_res['trajectory'].value}\n\n"
            "Session Dependency & Liquidity Sweep Status:\n"
            f"- Previous Day Context: {hourly_res['previous_day_status']}\n"
            f"- Analysis of the Active LOD/HOD Scenario: {hourly_res['scenario_details']}\n\n"
            "Time Cycle Execution & Imbalance Validation (15m/30m/90m):\n"
            f"- Retracements of 90m/30m Cycles into Prior Session Imbalances: {cycle_res['cycle_summary']}\n"
            f"- 30m Low/High Validation Point: {cycle_res['validation_summary']}\n\n"
            "Final Session Bias & Core Targets:\n"
            f"- Directional Bias: {final_bias.value} [{alignment_note}]\n"
            f"- Primary Liquidity & Imbalance Targets: {targets_str}"
        )

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


# ============================================================================
# EXAMPLE EXECUTION (SELF-TEST)
# ============================================================================

if __name__ == "__main__":
    sample_london_fvg = ImbalanceZone(
        imbalance_type="FVG",
        timeframe="30m",
        direction="BULLISH",
        upper_bound=20480.00,
        lower_bound=20450.00,
        origin_session="LONDON",
        swept_old_hl=True,
        dealing_range_pct=0.50,
    )

    sample_daily_fvg = ImbalanceZone(
        imbalance_type="FVG",
        timeframe="1D",
        direction="BEARISH",  # Premium target for bullish draw
        upper_bound=20850.00,
        lower_bound=20790.00,
        origin_session="HTF",
        swept_old_hl=False,
        dealing_range_pct=0.75,
    )

    sample_state = MarketStatePayload(
        current_price=20510.00,
        monthly_weekly_context={
            "PMH": 21100.00,
            "PML": 19850.00,
            "PWH": 20920.00,
            "PWL": 20410.00,
        },
        previous_day_context={
            "PDH": 20680.00,
            "PDL": 20425.00,
            "previous_day_delivery": "BEARISH_RETRACTION_INTO_HTF_DISCOUNT",
        },
        daily_trend_bias="BULLISH",
        htf_sweeps=[
            LevelSweepEvent(
                level_name="PWL",
                direction="LOW",
                price=20410.00,
                nq_swept=True,
                es_swept=False,  # NQ swept PWL, ES held above its PWL -> Intermarket SMT
                session="LONDON",
                smr_confirmed=True,
                cisd_confirmed=True,
            )
        ],
        session_sweeps=[
            LevelSweepEvent(
                level_name="PDL",
                direction="LOW",
                price=20425.00,
                nq_swept=True,
                es_swept=False,
                session="LONDON",
                smr_confirmed=True,
                cisd_confirmed=True,
            )
        ],
        imbalances=[sample_daily_fvg, sample_london_fvg],
        time_cycles=[
            TimeCycleEvent(
                cycle_minutes=90,
                extremum_type="LOW",
                price=20462.50,
                timestamp="09:30 EST",
                inside_imbalance=sample_london_fvg,
                local_smt_present=True,
            )
        ],
    )

    agent = OrderflowGeneralAgent()
    print(agent.analyze(sample_state))

