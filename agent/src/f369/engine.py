"""F369 Central Intelligence Engine.

The supreme state manager and validator. Owns the setup state machine,
validates every confirmation step in order, and is the only entity that may
declare a setup tradeable. Agents emit signals; the Engine decides.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from .bus import MessageBus
from .config import CONFIG
from .models import (
    AlertAuthorized,
    BacktestResult,
    BiasEstablished,
    Candle,
    ConfirmationStatus,
    Direction,
    Instrument,
    Message,
    PatternDetected,
    PatternSignal,
    PatternType,
    PerformanceSnapshot,
    RiskVerdict,
    Setup,
    SetupState,
    SharedContext,
    Timeframe,
)


@dataclass
class EngineEvent:
    event_type: str
    setup_id: Optional[str]
    payload: Dict[str, Any]
    timestamp: datetime = field(default_factory=datetime.utcnow)


class CentralEngine:
    """Singleton-like orchestrator. One instance per bot process."""

    def __init__(self, bus: Optional[MessageBus] = None) -> None:
        self.bus = bus or MessageBus()
        self.setups: Dict[str, Setup] = {}
        self.active_bias: Optional[BiasEstablished] = None
        self.pattern_history: List[PatternSignal] = []
        self.performance_metrics: Dict[str, float] = {}
        self._context_providers: List[Callable[[], SharedContext]] = []
        self._risk_provider: Optional[Callable[[], Dict[str, Any]]] = None
        self._alert_handler: Optional[Callable[[AlertAuthorized], Any]] = None
        self._setup_counter = 0
        self._lock = asyncio.Lock()

        self.bus.subscribe("bias.established", self._on_bias)
        self.bus.subscribe("pattern.detected", self._on_pattern)
        self.bus.subscribe("backtest.result", self._on_backtest)
        self.bus.subscribe("performance.snapshot", self._on_performance)
        self.bus.subscribe("risk.verdict", self._on_risk_verdict)

    def set_context_provider(self, provider: Callable[[], SharedContext]) -> None:
        self._context_providers.append(provider)

    def set_risk_provider(self, provider: Callable[[], Dict[str, Any]]) -> None:
        self._risk_provider = provider

    def set_alert_handler(self, handler: Callable[[AlertAuthorized], Any]) -> None:
        self._alert_handler = handler

    def get_shared_context(self) -> SharedContext:
        risk = self._risk_provider() if self._risk_provider else CONFIG.risk
        return SharedContext(
            setups=dict(self.setups),
            active_bias=self.active_bias,
            pattern_history=list(self.pattern_history),
            performance_metrics=dict(self.performance_metrics),
            risk_limits=risk,
            config=CONFIG,
        )

    def _new_setup_id(self) -> str:
        self._setup_counter += 1
        return f"setup_{self._setup_counter:06d}"

    def create_setup(self, instrument: Instrument = Instrument.NQ) -> Setup:
        setup = Setup(setup_id=self._new_setup_id(), instrument=instrument, state=SetupState.IDLE)
        self.setups[setup.setup_id] = setup
        return setup

    def get_setup(self, setup_id: str) -> Optional[Setup]:
        return self.setups.get(setup_id)

    def active_setups(self) -> List[Setup]:
        return [s for s in self.setups.values() if not s.is_terminal()]
    # ------------------------------------------------------------------
    # Message handlers (agents -> Engine)
    # ------------------------------------------------------------------
    async def _on_bias(self, bus_msg) -> None:
        payload = bus_msg.message.payload
        setup_id = payload.get("setup_id")
        async with self._lock:
            setup = self.setups.get(setup_id) if setup_id else None
            if setup is None:
                setup = self.create_setup()
                setup_id = setup.setup_id
            bias_dir = Direction(payload["direction"])
            self.active_bias = BiasEstablished(
                message_id=bus_msg.message.message_id,
                source_agent="general_agent",
                payload={
                    "setup_id": setup_id,
                    "direction": bias_dir.value,
                    "timeframe_alignment": payload.get("timeframe_alignment", {}),
                    "key_levels": payload.get("key_levels", {}),
                    "confidence": payload.get("confidence", 0.0),
                },
            )
            setup.bias = self.active_bias
            if setup.state == SetupState.IDLE:
                setup.transition(SetupState.MONITORING, "bias established")
        await self._emit(EngineEvent("bias_established", setup_id, {"direction": bias_dir.value}))

    async def _on_pattern(self, bus_msg) -> None:
        payload = bus_msg.message.payload
        setup_id = payload.get("setup_id")
        async with self._lock:
            setup = self.setups.get(setup_id)
            if setup is None:
                signal = self._build_signal(payload)
                self.pattern_history.append(signal)
                await self._emit(EngineEvent("pattern_unattached", None, signal.to_dict()))
                return

            signal = self._build_signal(payload)
            self.pattern_history.append(signal)

            # --------------------------------------------------------------
            # Signal routing — enforced sequence per the F369 workflow:
            #   MONITORING --(liquidity sweep + SMT)--> ACTIVE_WATCH
            #   ACTIVE_WATCH --(SMR)--> SMR_CONFIRMED
            #   SMR_CONFIRMED --(CISD)--> CISD_CONFIRMED
            #   CISD_CONFIRMED --(FVG/iFVG)--> ENTRY_READY
            #   ENTRY_READY --(Mindset PASS + final validation)--> ALERT_SENT
            # --------------------------------------------------------------
            routed = False
            if signal.pattern_type == PatternType.LIQUIDITY_SWEEP and setup.state == SetupState.MONITORING:
                await self._evaluate_liquidity_sweep(setup, signal)
                routed = True
            elif (
                signal.pattern_type == PatternType.SMT
                and setup.state in {SetupState.MONITORING, SetupState.ACTIVE_WATCH}
            ):
                await self._evaluate_smt(setup, signal)
                routed = True
            elif signal.pattern_type == PatternType.SMR and setup.state == SetupState.ACTIVE_WATCH:
                await self._evaluate_smr(setup, signal)
                routed = True
            elif signal.pattern_type == PatternType.CISD and setup.state == SetupState.SMR_CONFIRMED:
                await self._evaluate_cisd(setup, signal)
                routed = True
            elif signal.pattern_type in {PatternType.FVG, PatternType.iFVG} and setup.state == SetupState.CISD_CONFIRMED:
                await self._evaluate_fvg(setup, signal)
                routed = True
            if not routed:
                setup.notes.append(
                    f"{datetime.utcnow().isoformat()}: out-of-sequence {signal.pattern_type.value} "
                    f"at state {setup.state.value}"
                )

    async def _on_backtest(self, bus_msg) -> None:
        self.performance_metrics.update(bus_msg.message.payload.get("metrics", {}))
        await self._emit(EngineEvent("backtest_received", None, bus_msg.message.payload))

    async def _on_performance(self, bus_msg) -> None:
        self.performance_metrics.update(bus_msg.message.payload.get("metrics", {}))
        await self._emit(EngineEvent("performance_updated", None, bus_msg.message.payload))

    async def _on_risk_verdict(self, bus_msg) -> None:
        payload = bus_msg.message.payload
        verdict = payload.get("verdict")
        reason = payload.get("reason", "")
        setup_id = payload.get("setup_id")
        async with self._lock:
            setup = self.setups.get(setup_id) if setup_id else None
            if setup is None:
                return
            if verdict == "FAIL":
                setup.invalidate(f"risk: {reason}")
                await self._emit(EngineEvent("setup_invalidated", setup.setup_id, {"reason": f"risk: {reason}"}))
            elif verdict == "PASS" and setup.state == SetupState.ENTRY_READY:
                await self._finalize_setup(setup)

    # ------------------------------------------------------------------
    # State-machine transitions
    # ------------------------------------------------------------------
    async def _evaluate_liquidity_sweep(self, setup: Setup, signal: PatternSignal) -> None:
        setup.liquidity_sweep = signal
        setup.transition(SetupState.ACTIVE_WATCH, "liquidity sweep + SMT context received")

    async def _evaluate_smt(self, setup: Setup, signal: PatternSignal) -> None:
        if signal.confirmation_status != ConfirmationStatus.CONFIRMED:
            setup.invalidate(f"SMT not confirmed: {signal.confirmation_status.value}")
            return
        setup.smt_signal = signal
        setup.transition(SetupState.ACTIVE_WATCH, "SMT confirmed — setup now on active watch")

    async def _evaluate_smr(self, setup: Setup, signal: PatternSignal) -> None:
        if signal.confirmation_status != ConfirmationStatus.CONFIRMED:
            setup.invalidate(f"SMR not confirmed: {signal.confirmation_status.value}")
            return
        if setup.bias is None:
            setup.invalidate("SMR confirmed but no directional bias established")
            return
        setup.smr_signal = signal
        setup.transition(SetupState.SMR_CONFIRMED, "SMR confirmed vs bias")

    async def _evaluate_cisd(self, setup: Setup, signal: PatternSignal) -> None:
        if signal.confirmation_status != ConfirmationStatus.CONFIRMED:
            setup.invalidate(f"CISD not confirmed: {signal.confirmation_status.value}")
            return
        setup.cisd_signal = signal
        setup.transition(SetupState.CISD_CONFIRMED, "CISD confirmed")

    async def _evaluate_fvg(self, setup: Setup, signal: PatternSignal) -> None:
        if signal.confirmation_status != ConfirmationStatus.CONFIRMED:
            setup.invalidate(f"FVG/iFVG not confirmed: {signal.confirmation_status.value}")
            return
        setup.fvg_signal = signal
        setup.transition(SetupState.ENTRY_READY, "FVG/iFVG entry identified")

    async def _finalize_setup(self, setup: Setup) -> None:
        checks = [
            ("bias", setup.bias is not None),
            ("smt", setup.smt_signal is not None),
            ("smr", setup.smr_signal is not None),
            ("cisd", setup.cisd_signal is not None),
            ("fvg", setup.fvg_signal is not None),
            ("state_entry_ready", setup.state == SetupState.ENTRY_READY),
        ]
        failed = [name for name, ok in checks if not ok]
        if failed:
            setup.invalidate(f"final validation failed: {', '.join(failed)}")
            await self._emit(EngineEvent("setup_invalidated", setup.setup_id, {"failed": failed}))
            return

        setup.transition(SetupState.ALERT_SENT, "final validation passed — alert authorized")
        alert = AlertAuthorized(
            message_id=f"alert_{setup.setup_id}",
            source_agent="central_engine",
            payload={
                "setup_id": setup.setup_id,
                "instrument": setup.instrument.value,
                "bias": setup.bias.payload if setup.bias else {},
                "smt": setup.smt_signal.to_dict() if setup.smt_signal else None,
                "smr": setup.smr_signal.to_dict() if setup.smr_signal else None,
                "cisd": setup.cisd_signal.to_dict() if setup.cisd_signal else None,
                "fvg": setup.fvg_signal.to_dict() if setup.fvg_signal else None,
                "validated_at": datetime.utcnow().isoformat(),
            },
        )
        if self._alert_handler:
            await self._alert_handler(alert)
        await self.bus.publish(
            "alert.authorized", source_agent="central_engine", payload=alert.payload
        )
        await self._emit(EngineEvent("alert_authorized", setup.setup_id, alert.payload))

    def invalidate_setup(self, setup_id: str, reason: str) -> None:
        setup = self.setups.get(setup_id)
        if setup is None or setup.is_terminal():
            return
        setup.invalidate(reason)
        asyncio.create_task(self._emit(EngineEvent("setup_invalidated", setup_id, {"reason": reason})))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _build_signal(self, payload: Dict[str, Any]) -> PatternSignal:
        return PatternSignal(
            pattern_type=PatternType(payload.get("pattern_type", "smt")),
            instrument=Instrument(payload.get("instrument", "NQ")),
            timeframe=Timeframe(payload.get("timeframe", "1m")),
            timestamp=datetime.utcnow(),
            price_levels=payload.get("price_levels", {}),
            detection_criteria=payload.get("detection_criteria", ""),
            confirmation_status=ConfirmationStatus(payload.get("confirmation_status", "pending")),
            metadata=payload.get("metadata", {}),
        )

    async def _emit(self, event: EngineEvent) -> None:
        pass


engine = CentralEngine()

