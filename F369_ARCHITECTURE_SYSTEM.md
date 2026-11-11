# F369 AI Trading Bot — Central Intelligence Architecture

Replaces the default 3-agent board (Research Analyst, Risk Guard, Execution Agent) with a custom 6-agent swarm governed by a **Central Intelligence Engine**. Built on the original Time and Price by Zeussy (Frank 369) strategy; additions do not replace or simplify the original strategy.

---

## 1. System Topology

```
┌─────────────────────────────────────────────────────────────────────┐
│                     CENTRAL INTELLIGENCE ENGINE                      │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐ │
│  │ State Store │  │ Validator   │  │ Scheduler   │  │ Router      │ │
│  │ (Setup SM)  │  │ (Source of  │  │ (Event Loop)│  │ (Msg Bus)   │ │
│  └─────────────┘  │ Truth)      │  └─────────────┘  └─────────────┘ │
│  └─────────────────────────────────────────────────────────────────┘ │
│                    SHARED CONTEXT (read-only to agents)              │
└──────────────────────────────┬──────────────────────────────────────┘
                               │ structured messages (not decisions)
       ┌───────────┬───────────┼───────────┬───────────┬───────────┐
       ▼           ▼           ▼           ▼           ▼           ▼
  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
  │General  │ │Live     │ │Backtest │ │Data     │ │Mindset  │ │Alerts   │
  │Agent    │ │Agent    │ │Agent    │ │Analysis │ │Agent    │ │Agent    │
  └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘ └────┬────┘
       └───────────┴───────────┴───────────┴───────────┴───────────┘
                               ▼
                    Deterministic Detection Engine (code, not AI)
                    Data Sync Layer (NQ/ES, NY session, DST)
```

---

## 2. Core Rules & Constraints

| Rule | Enforcement |
|------|-------------|
| Centralized Validation | Central Engine is the single source of truth. Agents emit **signals**, never trade decisions. |
| No Override | No agent may transition a setup or declare a valid trade independently. |
| Shared Context | Agents receive a read-only view of the setup store, history, and metrics. |
| Specialized Scope | Each agent owns exactly one responsibility; cross-agent data flows through the Engine only. |
| Deterministic Detection | All pattern detection is mathematical (OHLCV + timestamps). AI explains, never guesses. |
| Synchronized Data | NQ and ES share timestamps, timeframe boundaries, NY session, DST handling. |
| Ordered Validation | Setup must pass all 6 confirmation steps in order; any failure invalidates the setup. |

---

## 3. Agent Responsibilities & Message Contract

| Agent | Input | Output (to Central Engine) |
|-------|-------|---------------------------|
| **General** | Daily / 1H / 15m candles | `BiasEstablished` (directional bias + HTF levels) |
| **Live** | Real-time NQ + ES 1m candles | `PatternDetected` (SMT, SMR, CISD, FVG/iFVG, liquidity sweeps) |
| **Backtest** | Historical OHLCV | `BacktestResult` (win rate, expectancy, edge metrics) |
| **Data Analysis** | Setup records + live metrics | `PerformanceSnapshot` (stats, equity curve, setup registry) |
| **Mindset** | Equity, drawdown, trade count | `RiskVerdict` (PASS/FAIL vs daily caps & drawdown limits) |
| **Alerts** | `AlertAuthorized` from Engine | Outbound notification (only after absolute validation) |

---

## 4. Setup State Machine (Central Engine)

```
IDLE
  │ [General establishes bias]
  ▼
MONITORING
  │ [Live detects liquidity sweep + SMT]
  ▼
ACTIVE_WATCH
  │ [Live detects 1m SMR; Engine verifies vs bias]
  ▼
SMR_CONFIRMED
  │ [Live detects CISD; Engine verifies]
  ▼
CISD_CONFIRMED
  │ [Live detects FVG/iFVG entry]
  ▼
ENTRY_READY
  │ [Engine final validation: bias + state + Mindset risk]
  ├─ PASS ───► ALERT_SENT ───► Alerts Agent notifies
  └─ FAIL ───► INVALIDATED ───► Engine notifies agents
```

Any condition invalidation at any state → `INVALIDATED`, setup archived, all agents notified.

---

## 5. Deterministic Detection Requirements (Configurable, Not Assumed)

The following are explicitly **not hardcoded** — they must be set in `config.py` before production:

1. **CISD**: candle body close vs wick break of opposing high/low.
2. **CISD opposing structure**: precise candle/structure selection rule.
3. **FVG/iFVG entry**: simple retracement vs additional in-zone confirmation.
4. **SMT/SMR swing-point matching**: exact lookback and tolerance rules.

Every detected pattern must carry: instrument, timeframe, timestamp, price levels, detection criteria, confirmation status. Missing data → `INSUFFICIENT_DATA` (never fabricate).

---

## 6. Data Synchronization

- NQ and ES candles aligned by UTC timestamp → converted to `America/New_York`.
- Same timeframe boundaries for both instruments.
- Session levels calculated in NY time.
- DST transitions handled via `zoneinfo`.
- Historical and real-time data processed identically.

---

## 7. Development Phases

1. Review original F369 spec → `docs/`
2. Integrate architectural requirements → this document
3. Design Central Engine → `engine.py`
4. Define agent communication → `bus.py` + `models.py`
5. Design deterministic detection → `detection.py`
6. Define shared data structures → `models.py` + `sync.py`
7. Design trade validation state machine → `engine.py`
8. Backtesting reproducibility → `backtest.py` uses same `detection.py`
9. Identify ambiguous rules → `config.py` placeholders
10. Present architecture & roadmap → this document
