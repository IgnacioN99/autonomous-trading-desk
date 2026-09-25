# Funding Rate Arbitrage & Liquidation Heatmaps

## 1. Delta-Neutral Cash-and-Carry Strategy
The strategy with the highest documented Sharpe Ratio in crypto futures literature (Sharpe > 4.5, Drawdown < 2%):
- Buy the underlying spot asset (Spot BTC/ETH).
- Open an equivalent short position (1x Short) in Perpetual Futures.
- Systematically harvest the Funding Rate every 8 hours whenever perpetuals trade at a premium over spot.
- Expected Yield: 15% to 55% APR with zero net directional exposure ($\Delta = 0$).

## 2. Liquidation Heatmap Cluster Dynamics
Price clusters where dense volumes of estimated liquidation stops reside act as liquidity magnets:
1. **Attraction Phase:** Price is mechanically drawn into dense liquidation clusters to fill forced market orders.
2. **Exhaustion Phase:** Once the liquidation cluster is fully absorbed, unless fresh spot buying emerges, an immediate mean reversion unfolds.
3. **Operational Rule:** Never chase breakout momentum that has just plowed through a major liquidation cluster; target mean reversion back toward EMA 20 post-absorption.
