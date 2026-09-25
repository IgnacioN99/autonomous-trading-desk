# Kelly Criterion & Quantitative Risk Management in Crypto Derivatives

## 1. Mathematical Foundations of the Kelly Criterion
The Kelly Criterion determines the optimal fraction of capital ($f^*$) to risk across a sequence of trades to maximize the long-term logarithmic capital growth rate:

$$f^* = \frac{b \cdot p - q}{b} = p - \frac{q}{b}$$

Where:
- $p$: Empirical win probability (Win Rate).
- $q$: Loss probability ($1 - p$).
- $b$: Payoff ratio (R:R or Average Win / Average Loss).

### The Peril of Full Kelly in Cryptoassets
In markets characterized by extreme fat tails, execution slippage, and regime shifts, Full Kelly mathematically causes unacceptable drawdowns (>50%) or ruin due to win-rate estimation error.

Therefore, institutional quantitative standards mandate Fractional Kelly:
- **Half-Kelly ($f^* / 2$):** Captures 75% of optimal growth while slashing portfolio variance by 50%.
- **Quarter-Kelly ($f^* / 4$):** Mandatory baseline for micro accounts (<$1,000 USD) on Binance Futures, capping nominal per-trade risk at 1.0% - 2.5% of total capital.

## 2. Position Sizing for Micro Accounts (Binance Futures)
For a micro account ($C = 100 - 500$ USDT):
- Maximum Risk per Trade ($R_{\text{trade}}$): 2.0% of account equity ($2 to $10 USDT maximum loss).
- Contract Notional Sizing Formula:
  $$\text{Notional (USDT)} = \frac{R_{\text{trade}}}{\text{SL Distance (\%) trick}}$$
  $$\text{Required Margin (USDT)} = \frac{\text{Notional (USDT)}}{\text{Leverage}}$$

### Golden Rule: Mandatory Isolated Margin
- Every intraday position and especially high-volatility moonshots (10x-15x YOLO memecoins) MUST execute under ISOLATED margin.
- Total wallet balance must never cross-collateralize an active position. In the event of catastrophic gap down or flash crash, losses are physically constrained by software to the assigned isolated margin.

## 3. Ruin Prevention & Daily Circuit Breakers
1. **Three-Strikes Rule:** 3 consecutive intraday losses trigger an automatic 12-hour terminal shutdown.
2. **Maximum Daily Drawdown:** 4.0% - 5.0% of account balance. If touched, cancel all active limit orders and flatten directional exposure.
3. **Capital Velocity Recycling:** Force-cancel unfilled limit orders after 60-90 minutes to free up margin.
