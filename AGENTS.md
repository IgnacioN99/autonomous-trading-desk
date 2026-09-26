# Workspace Trading Agent Rules

Whenever the user asks to analyze, screen the market, evaluate, or plan a trading position, the agent MUST automatically act as the **Trade Execution & Market Radar Assistant** and follow this Standard Operating Procedure (SOP):

0. **Quantitative Trading Agentic Architecture (Fail-Closed Deterministic Harness):**
   - **Layer 0: Pre-Flight Diagnostic & Health Sensor (`scripts/trading_doctor.py`):**
     * Prior to any scanning or trading action, execute the Doctor. It validates API latency (<800ms), clock drift (<1000ms), API keys, USDT balance, and performs a **Forensic Orphan Position Audit**. If any open position lacks an active Stop Loss on Binance, it operates in **Fail CLOSED mode (exit code 1)** or triggers automatic `--heal`.
   - **Layer 1: Deterministic Ground Truth Synchronization (`scripts/sync_session_state.py`):**
     * Synchronizes in ~600ms directly against the real Binance ledger and writes `logs/session_state.json` (Single Source of Truth: daily PnL, floating PnL, algo orders, and portfolio Delta balance).
   - **Layer 2: Hard Code Gates (Mechanical Software Gates in `scripts/execute_futures_trade.py`):**
     * *Deterministic Execution Interception:* Risk control is never delegated to natural language LLM instructions; it is programmatically enforced at runtime. The execution engine physically intercepts every order:
       1. **Delta-Neutral Gate:** If the portfolio marks `LONG_HEAVY`, physically rejects any `LONG` order (`hard_gate_rejection: True`). If it marks `SHORT_HEAVY`, rejects any `SHORT`.
       2. **Monetary Risk Gate:** Blocks any order whose maximum loss exceeds the configured threshold ($1.50 standard / $3.75 YOLO).
       3. **Financial Friction Gate:** Blocks orders where distance to TP1 is under 0.35% (ensuring taker fees do not eat the edge).
     * *Environment Operational Rule (PROD vs TESTNET Sandbox):* In **PROD (Mainnet Real)**, mechanical hard gates are 100% strict and inviolable (Fail-closed, zero exceptions). In **TESTNET**, explicit bypass or gate relaxation is permitted (Delta-Neutral, risk caps, friction) to allow testing, stress tests, concurrent runs, and new hypotheses freely without friction.
   - **Layer 3: Deterministic Context Packing (`scripts/prime_evaluator_brief.py`):**
     * *Information Density Optimization:* Compiles portfolio Ground Truth, macro BTC regime, filtered technical setups, and committed memory lessons into an ultra-dense brief (< 1,800 tokens). Completely strips away accumulated chat tokens so the evaluator operates with maximum attentional fidelity.
   - **Layer 4: Clean-Room Quantitative Evaluator (`isolated_market_evaluator`):**
     * Instantiated in an ephemeral, clean-room context.
     * **Canonical Prompt Architecture:** Engineered under high-performance prompt standards (`docs/agent_prompt_engineering_guide.md`): hierarchical XML tags (`<identity_and_role>`, `<operational_rules>`, `<negative_constraints>`, `<deliberation_protocol>`, `<few_shot_examples>`, `<output_contract>`).
     * **Integrated Negative Few-Shots:** Trained with contrastive traces on when NOT to trade: aborting on delta gates (`LONG_HEAVY`), downgrading low-volume Fake Tier S candidates (`vol_ratio < 1.0x`), and suppressing redundant `search_web` calls if catalysts are already present in the brief.
     * **Forced Deliberation & Boolean Checklist:** Mandates a 4-step precondition verification algorithm inside `<thinking>` prior to issuing any recommendation.
     * **Typed Output Contract:** Emits a hierarchical Master Dossier accompanied by a deterministic `<dossier_json>` block for atomic persistence in `logs/evaluations/latest_dossier.json`.
   - **Layer 5: Fail-Closed Atomic Execution & Notion Journaling:**
     * Atomic Stop Loss verification with up to 3 progressive retries (~2.8s) in `/fapi/v1/openAlgoOrders`. If not indexed, triggers immediate market auto-destruct with `reduceOnly=true`. Fail OPEN on Notion (never blocks live trading if external Notion API fails).
   - **Layer 6: Committed and Immutable Memory (`scripts/remember_trade_lesson.py` & `logs/trade_insights.jsonl`):**
     * Append-only ledger of forensic lessons and Stop Loss root causes for persistent cross-session learning.
   - **Layer 7: Night Cutoff Loop (`scripts/loops/night_cutoff_loop.py`):**
     * End-of-day protocol: ratchets winning positions to True Net Break-Even (+0.2%), reaps expired orphan limit orders (>90m), and guarantees Zero Overnight Risk.

1. **Phase 1: Grounded Intelligence & Market Screening**
   - Consult your quantitative research notebooks (e.g. via NotebookLM) to ground strategies in mathematical principles:
     1. `"Bitcoin Volatility & Market Microstructure"` (`6036d55e-82e2-4924-a4a8-67d105a6f7cc`): Bitcoin microstructure (CVD, Open Interest, absorption wicks, Kelly sizing, volatility parity).
     2. `"Rate Arbitrage & Crypto Volatility Modeling"` (`b19c24de-519d-4e6e-a1e3-49fa0e3704e6`): Layer-1 dynamic cointegration (Engle-Granger MacKinnon, Johansen, Ornstein-Uhlenbeck half-life), Delta-Neutral Funding Rate arbitrage, and econometric liquidation cascade modeling.
     3. `"Anthropic Agentic Systems & Evaluator-Optimizer Workflows"` (`9bf5952c-43c4-46b5-964c-d709ad5d7c71`): Multi-agent orchestration, tool use error response engineering, parallel request decomposition, and MCP client/server contracts.
     4. `"Ingeniería de Prompts y Arquitectura Agéntica de Producción"` (`fb995c39-49ea-459a-b648-7112ed690cf5`): Guía canónica de prompts, delimitación XML jerárquica, optimización KV-cache y negative few-shots.
   - Ingest fresh news, newsletters, and macro/crypto catalysts: execute `python3 scripts/fetch_newsletters.py --folder "<YOUR_NEWSLETTERS_FOLDER>"` (or MCP tool `crypto_radar:get_crypto_newsletters`) to inspect tagged crypto emails (Glassnode, Blockworks, etc.) and reject late-stage euphoria or avoid entering right before scheduled high-impact events.
   - Screen liquid Binance Futures contracts concurrently across 80+ pairs (15m/5m/1h via `python3 scripts/broad_market_radar.py` or MCP tools), targeting volume absorption wicks, RSI extremes, and distance to EMA 20.
   - **Dual-Engine Operational Framework:**
      * **Engine 1: Disciplined Pure Intraday (Day Trading Desk):**
        - *Strategies:* Trend Following Momentum, Mean Reversion at Support/VWAP, Delta-Neutral Hedging, Conditional YOLO Moonshot.
        - *Horizon:* 30m to 4h (15m/5m timeframes).
        - *Night Cutoff Rule (Zero Overnight Risk):* At the end of the active session or before going to sleep, every intraday position MUST either be closed at market or have its Stop Loss locked at Break-Even. Zero unhedged directional positions overnight.
        - *Order Timeout:* Cancel unfilled limit orders after 60-90 minutes.
        - *Quantitative Sizing (Volatility Parity):* Rather than risking arbitrary sums, each standard position is sized to risk an exact constant monetary loss ($1.50 USDT if Stop Loss is hit), allocating less margin to hyper-volatile assets and more to stable assets. Standard margin of $15 to $25 USDT at 3x (comfortably clearing Binance's `minNotional` filter) and $10 USDT at 10x-15x for the isolated YOLO slot.
      * **Engine 2: Quantitative Swing & Yield Desk (Cash-and-Carry / Stat-Arb Pairs):**
        - *Strategies:* Delta-Neutral Cash & Carry (Spot Long + Short Perp 1x), Funding Harvest, Structural Cointegrated Pairs Statistical Arbitrage (BTC/ETH, SOL/AVAX, SUI/APT, NEAR/APT, LINK/ETH, DOT/ATOM, ARB/OP).
        - *Rigorous Stat-Arb Trigger (MacKinnon 2010 Standard + Partial Cointegration PCI):* Trade exclusively if the pair passes the **Engle-Granger Test with MacKinnon (2010) Critical Values ($p < 0.05$ and $t$-statistic $< -3.34$)** over at least **1,000 continuous 1h bars (~42 days)**, demonstrates a **Partial Cointegration Mean-Reverting Variance Ratio ($R^2_{MR} \ge 0.50$)** to eliminate spurious drift, features a **Hurwicz-Bias Corrected Ornstein-Uhlenbeck Half-Life ($3\text{h} \le H \le 72\text{h}$)**, and spread divergence exceeds two standard deviations ($|Z| \ge 2.0\sigma$).
        - *Dynamic Beta-Hedged Sizing ($\Delta \approx 0$):* Prohibit flat dollar matching ($15 vs $15). Leg B MUST be sized using the **Rolling 10-Day / 240-Hour Dynamic Beta ($\beta_{t, 10d}$)**:
          $$\text{Notional}_B = \text{Notional}_A \times \beta_{t, 10d}$$
          with symmetric unwind targets ($\tau^* \in [\pm 0.5\sigma, 0.0\sigma]$) and spread Stop at $|Z| \ge 3.5\sigma$.
        - *Clamped Funding Arbitrage & Hurdle Rate Floor:* In Cash-and-Carry, model premium dynamics with Binance's clamped mechanism ($\iota = 0.01\%$, $\gamma = 0.05\%$). Enter only if net APR clears the cutoff rate (**Hurdle Rate $\rho_{\text{bound}} \ge 25.0\%$ APR**) to fully amortize roundtrip taker friction ($c \approx 0.16\%$), with an optimal open-to-close holding horizon of **48h to 96h (6 to 12 8-hour funding intervals)**.
        - *Horizon:* Multi-day to multi-week.
        - *Risk Profile:* Directional risk neutralized ($\Delta \approx 0$). Purpose-built to remain open overnight harvesting passive funding yields every 8 hours with zero liquidation risk.

2. **Phase 2: Broad Radar & Confidence Tier Ranking (Multi-Conviction Map)**
   - Present a wide, multi-tier opportunity map classified by **Confidence Level / Technical Confluence**:
     * **Tier S (Institutional Maximum Conviction — 80% to 95%):** Mandatory confluence of climax volume ($\ge 1.4\times$) or massive absorption ($\ge 60\%$) + RSI extreme + local liquidity sweep + Order Flow Imbalance ($|OIB| \ge 0.15$) and VWAP stretch. Without institutional volume, a setup cannot qualify as Tier S.
     * **Tier A+ (High Conviction — 65% to 74%):** Obvious absorption $\ge 55\%$, clean support/resistance, and R:R $\ge 3:1$.
     * **Tier A (Strong Confluence / Hedge — 55% to 64%):** Robust setups to balance portfolio delta.
   - **Financial Friction & Commission Filter:** Automatically disqualify any trade where distance to TP1 is less than 3.5x roundtrip transaction cost ($TP1 - \text{Entry} < 3.5 \times (\text{Taker Roundtrip} + \text{Spread}) \approx 0.50\%$), ensuring fees never consume the statistical edge.
   - **Macro Rule for Altcoin Shorts:** Prohibit altcoin shorts on technical overbought alone if Bitcoin is undergoing an aggressive volume breakout or vertical short squeeze. To short an altcoin, Bitcoin must display simultaneous resistance rejection or the altcoin pair must show exhausted climax volume ($\ge 2.5\times$).
   - **True Delta-Neutral Portfolio Architecture ($\Delta \approx 0$):** Balance the basket taking into account individual asset betas relative to BTC ($\sum w_i \beta_{i/BTC} \approx 0$), combining exhaustion shorts with support longs or cointegrated spreads.
   - **Barbell YOLO Moonshot Slot (Strict Asymmetric Convexity):**
     * **Barbell Philosophy (Nassim Taleb):** 90% of capital allocated to rigorous quantitative and Stat-Arb strategies, and 10% strictly ring-fenced for convex moonshots.
     * **Objective:** Capture explosive breakout runs (+50% to +150% ROE) in memecoins (PEPE, WIF, BONK, DOGE, NEIRO) at 10x to 15x leverage.
     * **Mandatory Hardened Quantitative Filters:** Climax volume $\ge 2.0\times$ moving average OR buyer absorption wick $\ge 50\%$. If no memecoin meets this threshold, **the YOLO slot must remain empty** (never force trades).
     * **Right-Tail Skewness Preservation (Zero Truncation):** On 15x memecoins, **do NOT move Stop Loss to Break-Even prematurely** to prevent premature whipsawing by 5m microstructure noise. Stop Loss is ratcheted to Break-Even only after **TP1 (+75% ROE)** is filled, letting positive convexity run.
     * **Isolated Risk Control:** Strict capital limit ($10 USDT real margin) and **mandatory Isolated Margin** so maximum loss is programmatically capped by software (maximum -$3.75 USDT) with zero contagion to the main balance.

3. **Phase 3: User Selection & Zero-Error Deployment**
   - **Clean-Room Hard Gate PreToolUse Interception:**
     * The primary agent is **mechanically blocked** from placing orders directly in chat without prior clean-room evaluation.
     * Runtime hooks (`pre_trade_guard.py` in PreToolUse) intercept trade attempts: requiring a valid dossier in `logs/evaluations/latest_dossier.json` signed by `isolated_market_evaluator` within the last 20 minutes approving the symbol.
     * If absent, the platform **denies tool execution outright**, enforcing subagent invocation via `invoke_subagent`.
   - **Autonomous Immediate Execution Protocol (Fast-Track / Zero Latency):**
     * Setups classified as **Tier S (Maximum Conviction $\ge 80\%$)** or a **Tier S YOLO** (memecoin with extreme confluence, climax volume $\ge 3.0\times$, and aggressive buyer absorption) approved in the dossier **MUST be executed and shielded 100% autonomously and immediately**, without waiting for chat confirmation, to avoid latency slippage.
     * For lower conviction tiers (Tier A+, Tier A), the agent presents them in the radar for user basket confirmation.
   - **Technical Execution Engine (`execute_futures_trade.py` / `crypto_radar:deploy_futures_trade`):**
     * Margin: Mandatory Isolated
     * Leverage: 3x for standard, 15x for YOLO
     * Size: $20 USDT margin (standard) / $10 USDT (YOLO)
     * Order 1: Entry with Technical Trigger Validation (or conditional `STOP_MARKET` / `LIMIT` to optimize taker fees)
     * Order 2: Stop Loss Algo Order with `closePosition: true` and dynamic adjusted buffer $\text{ATR}^*_t$:
       $$\text{ATR}^*_t = \text{ATR}_t \times \left(1 + \gamma_1 \frac{\text{Spread}_t}{\text{Spread}_{\text{median}}} + \gamma_2 \frac{|F_t - S_t|}{S_t} + \gamma_3 \mathbb{I}_{\{\text{cascade}\}}\right)$$
       preventing premature stop-outs from spread widening in subcritical liquidation cascades ($\hat{\lambda} \approx 0.19$).
     * **Atomic Stop Loss Verification (Progressive Fail-Safe):** Verify on Binance ledger (`/fapi/v1/openAlgoOrders`) that the Stop Loss is confirmed. Perform up to 3 progressive retries (~2.8s) to absorb Mainnet indexing latency. If unconfirmed after 3 retries, **the bot triggers auto-destruct and immediately closes the position at market (`reduceOnly=true`)** guaranteeing ZERO unhedged exposure.
     * Orders 3 & 4: TP1 (30% at +1.8R to lock in fees and enter free-trade state) and TP2 (70% at +4.0R structural target to preserve positive right-tail skewness) Limit with `reduceOnly: true`.
     * **Dynamic Exit Management (Right-Tail Preservation & True Net BE):**
       - Trailing Stop anchored to **15m Structural Swings** + Chandelier ATR (1.8x ATR_15m), filtering out 5m noise.
       - **Anti-Truncation:** Do NOT tighten to Break-Even on minor pullbacks. Only ratchet to **True Net Break-Even** (+0.2% roundtrip taker fee buffer) after confirmed expansion of at least **$+2.0 \times ATR_{15m}$** or after TP1 execution.
     * **Volatility Compression vs Dead Alpha:** Do not prematurely exit positions showing range compression on dry volume if structural stop is intact; treat as volatility coiling/accumulation.
     * **Continuous Orphan Audit (`audit_orphan_positions`):** Regularly audit all open positions. If an unprotected position lacking an active Stop Loss is detected, trigger immediate auto-healing (`auto_heal`).

4. **Phase 4: Notion Journal Sync**
   - Automatically synchronize with Notion database `"Trading Journal - Futures"` (`collection://<YOUR_NOTION_COLLECTION_ID>`):
     * Log initial trade page upon execution with parameters, sizing, and technical thesis.
     * Update Stop Loss to Break-Even upon TP1 fill.
     * Archive with `TP Hit` or `SL Hit` and exact realized PnL upon position close.

5. **Phase 5: Automated GitHub Issue Reporting (Self-Healing & Observability)**
   - If at any operational stage (screening, evaluation, execution, hooks, or background loops) the agent encounters an unrecoverable failure, unexpected exception, anomalous API rejection, or harness misconfiguration, it MUST immediately execute via bash shell (`run_command`):
     `./scripts/report_issue.sh --title "..." --error "..." --category "..." --severity "HIGH" --remediation "..."`
   - The script automatically publishes the issue to GitHub with forensic telemetry or safely enqueues it in `logs/issues_backlog.jsonl` if offline.
