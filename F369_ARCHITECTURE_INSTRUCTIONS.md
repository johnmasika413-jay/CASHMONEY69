# F369 AI Trading Bot — Architecture and Development Update

These requirements are additions to the original Time and Price by Zeussy (Frank 369) strategy specification. They do NOT replace or simplify the original strategy.

## 1. Central Intelligence System

The six agents must NOT operate as independent systems making their own trading decisions.

Agents:
1. General Agent
2. Live Agent
3. Backtest Agent
4. Data Analysis Agent
5. Mindset Agent
6. Alerts Agent

Build one **central Market Analysis Engine** that coordinates all six agents. The central engine must:
- Collect and distribute market data.
- Maintain the current market narrative.
- Track higher-timeframe liquidity and imbalances.
- Coordinate information between all six agents.
- Track the progress of every potential trading setup.
- Validate whether all strategy conditions have been satisfied.
- Ensure every agent works with the same market information.

Each agent has a specialized responsibility but shares relevant information with the central engine. No agent may independently override the central engine's trade validation rules.

Example workflow:
- General Agent establishes the daily directional bias using the daily, 1-hour and 15-minute charts.
- Live Agent monitors NQ and ES and identifies liquidity sweeps and SMT.
- Central Engine receives the SMT information and updates the setup state.
- Live Agent continues monitoring for 1-minute SMR.
- Central Engine verifies the SMR.
- Live Agent identifies CISD.
- Central Engine verifies CISD.
- Live Agent identifies an FVG or iFVG entry opportunity.
- Alerts Agent sends a notification only when the required conditions are satisfied.

The other agents must also have access to relevant historical information and setup records.

## 2. Deterministic Price-Action Detection

Do NOT use an AI that looks at a chart and guesses whether SMT, SMR, CISD or an FVG exists. Build a deterministic price-action detection engine using actual market data and mathematical conditions.

The system must process:
- Actual candle open, high, low, close prices.
- Candle timestamps.
- Swing highs and swing lows.
- Corresponding NQ and ES price movements.
- Liquidity levels.
- Imbalance boundaries.
- Candle formations and structural breaks.

Pattern detection requirements:
- **SMT:** Compare corresponding NQ and ES liquidity levels and determine which instrument took liquidity and which failed to take it.
- **SMR:** Calculate and compare actual 1-minute swing highs and lows between NQ and ES.
- **CISD:** Identify the relevant opposing candle or structure and verify the required price break using the exact CISD definition configured for the strategy.
- **FVG and iFVG:** Calculate actual three-candle imbalance boundaries and monitor their mitigation and inversion status.

The AI language model should explain detected patterns, summarize market conditions, and coordinate the agents. The actual pattern detection must be handled by code, not subjective AI interpretation.

Every detected pattern must include: Instrument, Timeframe, Timestamp, Relevant price levels, Detection criteria, Confirmation status. If data is missing or insufficient, the system must report that it cannot confirm the pattern. Never fabricate market information or generate signals from incomplete data.

## 3. Central Trade Validation Engine

Build a single source of truth for trade validation. Every setup must follow the original strategy in the correct order:
1. Daily, 1-hour and 15-minute directional analysis.
2. Price reaches important higher-timeframe liquidity or imbalance.
3. SMT confirmation between NQ and ES.
4. 1-minute SMR confirmation.
5. 1-minute CISD confirmation.
6. FVG or iFVG entry opportunity.

The engine must maintain the state of each setup and communicate it to all six agents. No agent can skip a confirmation or independently declare a valid trade. If any condition becomes invalid, the engine must update the setup status and notify the relevant agents.

## 4. Data Synchronization

Because the strategy depends heavily on comparing Nasdaq and S&P 500, accurate synchronization is essential:
- NQ and ES use synchronized timestamps.
- Both instruments use the same timeframe boundaries.
- Candle data is aligned correctly.
- Session levels are calculated using America/New_York time.
- Daylight saving changes are handled automatically.
- Historical and real-time data are processed consistently.

## 5. Development Approach

Before implementing the complete system:
1. Review the original F369 strategy specification.
2. Integrate these architectural requirements.
3. Design the central Market Analysis Engine.
4. Define communication between the six agents.
5. Design the deterministic pattern detection algorithms.
6. Define the data structures shared between agents.
7. Design the trade validation state machine.
8. Explain how historical backtesting will reproduce the same detection logic used in live markets.
9. Identify any remaining ambiguities in the trading rules.
10. Present the architecture and development roadmap.

Do not immediately write a massive amount of code without establishing the architecture first. Build and test each module individually.

## 6. Do Not Assume Undefined Strategy Rules

Do not invent these rules; request explicit confirmation before finalizing detection algorithms:
- Whether CISD requires a candle body close or whether a wick breaking the relevant high or low is sufficient.
- The precise rules for identifying the relevant opposing candle or structure for CISD.
- Whether an FVG/iFVG entry requires a simple retracement into the zone or additional confirmation inside the zone.
- The exact swing-point matching rules for SMT and SMR.

## 7. Final Objective

F369 must function as one intelligent trading ecosystem:
- The six agents work together through a central intelligence system.
- The detection engine uses actual mathematical price-action rules.
- The AI understands and explains the market using the Time and Price methodology.
- The bot is patient, objective and disciplined. It never forces a setup, invents confirmations, or generates a trade simply because the market is moving.
- The strategy remains the foundation; the technology adapts to the strategy.
- Analyze first, confirm second, alert only when the complete setup is present.

## Dashboard Access

Pixel Office dashboard URL (localhost): http://127.0.0.1:5899/pixel-office

The frontend dev server (port 5899) proxies /api to the backend at http://127.0.0.1:8899. Both servers must be running to access the dashboard. State is stored in pixel-office.sqlite3 under ~/.vibe-trading.

Never compare unrelated swing points or mismatched candle periods when identifying SMT and SMR.