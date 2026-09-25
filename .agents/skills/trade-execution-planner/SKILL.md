---
name: trade-execution-planner
description: >
  Crypto trade execution planner, market radar, and risk manager. Grounded in
  quantitative volatility research (e.g. via NotebookLM: <YOUR_NOTEBOOKLM_NOTEBOOK_ID_1>).
  Screens the live Binance Futures market, ingests news/catalysts, presents a ranked
  TOP opportunities list by probability/confluence, and assists in selecting,
  deploying with zero-error Binance fields, and syncing to the Notion Trading Journal.
---

# Crypto Trade Execution Planner & Market Radar

This skill operationalizes a systematic, grounded framework for crypto futures trading.

## When to Use This Skill
Activate whenever the user:
- Asks to scan the market, find trades, or open new positions.
- Shares news, newsletters, or market catalysts.
- Requests a top list or ranking of high-probability setups.
- Wants execution parameters for Binance Futures and synchronization with Notion.

## Phase 1: Grounded Research & Market Screening
1. Consult quantitative research notebooks (e.g. via NotebookLM: `<YOUR_NOTEBOOKLM_NOTEBOOK_ID_1>`) for mathematical rules on candlestick absorption wicks, Spot CVD divergence, Open Interest washouts, and Kelly / Volatility Parity sizing.
2. Ingest fresh newsletters & macro catalysts: run `python3 scripts/fetch_newsletters.py --folder "<YOUR_NEWSLETTERS_FOLDER>"` to ingest research feeds (Glassnode, Blockworks, etc.) and reject late-stage euphoria or avoid entering right before scheduled high-impact events.
3. Screen top liquid Binance Futures pairs (RSI, distance to 24h lows/highs, volume wicks, EMA 20/50).

## Phase 2: TOP 5-6 Opportunities Ranking
Generate a clear, ranked table with 5 to 6 setups ordered by confluence and probability:
- **Rank & Asset:** e.g., #1 SUIUSDT, #2 BTCUSDT...
- **Direction:** Long / Short
- **Probability / Confluence Tier:** High (Confluence score 85%+), Medium-High, etc.
- **Strategy Selected:**
  * Mean Reversion (1h RSI < 25 + volume absorption wick)
  * Pullback to Support / S-R Retest (order limit on confirmed support)
  * Breakout & Retest (volume breakout above 4h resistance)
- **Levels:** Entry, Stop Loss, TP1 (50% size / EMA 20), TP2 (50% size / Structural target)
- **Sizing & Returns:** $20 USDT margin, 3x for majors / 2x for low-caps, R:R ≥ 2:1, ROE %.

## Phase 3: User Selection & Zero-Error Deployment
1. The user selects which setup(s) to trade from the TOP ranking.
2. Provide the field-by-field checklist for Binance Futures:
   - Margin: Isolated
   - Leverage: 3x or 2x
   - Currency unit: USDT vs Token check
   - Order 1: Entry + SL
   - Order 2: TP1 Limit with `Reduce-Only: Checked`
   - Order 3: TP2 Limit with `Reduce-Only: Checked`

## Phase 4: Notion Journal Sync
Sync the chosen position to Notion:
- Database: `Trading Journal - Futures` (`collection://<YOUR_NOTION_COLLECTION_ID>`).
- Include all parameters, strategy notes, and execution status (`Open` or `Pending`).
