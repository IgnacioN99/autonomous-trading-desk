# 🅿️ Parking Lot — Immutable Registry of Deferred Strategies & Ideas

This file serves as the formal repository for quantitative hypotheses, architectures, and strategies that are **outside current immediate scope**, but must be preserved intact without causing cognitive noise or context rot in daily trading sessions.

---

## 💡 1. Deferred Quantitative Strategies

### [PL-01] Tripartite Funding Rate Arbitrage (Multi-Exchange Funding Harvest)
* **Date:** 2026-09-23 | **Status:** `DEFERRED`
* **Hypothesis:** Long Spot (or Perp on exchange with negative funding) vs Short Perp on Binance (extreme positive funding). Harvest passive yield every 8 hours with zero directional risk ($\Delta = 0$).
* **Requirements:** Integration with secondary exchange (Bybit / Hyperliquid / OKX) and atomic cross-API order routing.
* **Activation Trigger:** When annualized net funding APR clears 25% for more than 3 consecutive days.

### [PL-02] Cointegrated Stat-Arb with Adaptive Kalman Filter
* **Date:** 2026-09-23 | **Status:** `UNDER EVALUATION`
* **Hypothesis:** Replace static OLS beta regression ($\beta_{A/B}$) with a state-space Kalman Filter that continuously updates hedge ratios bar-by-bar to absorb structural volatility shifts.
* **Requirements:** Implement `pykalman` or custom Bayesian recursive filter in `scripts/quant_risk_engine.py`.

### [PL-03] L2 Microstructure: Order Flow Toxicity (VPIN - Volume-Synchronized Probability of Toxicity)
* **Date:** 2026-09-23 | **Status:** `DEFERRED`
* **Hypothesis:** Compute VPIN across constant volume buckets to detect institutional order flow toxicity and anticipate support breakdown before appearance on 15m candlesticks.

---

## 🛠️ 2. Infrastructure & Automation

### [PL-04] Direct WebSocket Feed for Zero-Latency Trailing Stop
* **Date:** 2026-09-23 | **Status:** `DEFERRED`
* **Hypothesis:** Replace REST price polling with a direct WebSocket listener at `wss://fstream.binance.com/ws/!miniTicker@arr` to update structural trailing stops sub-100ms.
* **Risk:** Requires managing a persistent background daemon process.

### [PL-05] Execution Push Notifications & Webhooks (Telegram / Discord)
* **Date:** 2026-09-23 | **Status:** `READY FOR IMPLEMENTATION`
* **Hypothesis:** Dispatch an immediate concise alert whenever Fast-Track places a trade, a TP1 is hit, or the Night Cutoff Loop locks a position to Break-Even.

---

## 📈 3. Capital Management & Scaling Rules

### [PL-06] Mainnet Transition with Phased Risk Scaling
* **Date:** 2026-09-23 | **Status:** `DEFERRED`
* **Graduation Criteria:** Requires accumulating at least 50 audited trades on Testnet with Sharpe Ratio $> 1.4$, Max Drawdown $< 5\%$, and zero orphan Stop Loss failures.

---

*Rule: Every new idea is appended to the bottom without editing prior entries. When an idea is implemented, it is marked as `GRADUATED` citing the relevant PR or commit.*
