# Liquidity Sweep Neutralization (Stop Hunts) & Quantitative Buffers

## 1. Anatomy of a Liquidity Sweep in Cryptoassets
Cryptocurrency derivatives markets exhibit pronounced microstructure asymmetry. Market makers and high-frequency algorithms routinely target Stop Loss clusters located mechanically beneath visible chart support or above resistance.

### Why Immediate Entries Fail:
Entering at the exact rejection wick or on the close of the 15m candle leaves traders vulnerable to the secondary confirmation sweep (a second wick extending several pips further to trigger late stops).

## 2. Next-Candle Confirmation Trigger Filter
Grounded in empirical order flow microstructure and technical price action:
1. **Entry Rule:** NEVER enter at market upon the close of the pattern formation candle.
2. **Mandatory Trigger:**
   - For LONG: The subsequent candle must break above the absolute high of the pattern candle ($\text{High}_{\text{trigger}} = \text{High}_{\text{pattern}} + 0.05\%$).
   - For SHORT: The subsequent candle must break below the absolute low of the pattern candle ($\text{Low}_{\text{trigger}} = \text{Low}_{\text{pattern}} - 0.05\%$).
   - If the subsequent candle fails to breach the extreme and pulls back, the order is immediately cancelled, neutralizing the stop hunt.

## 3. Dynamic Stop Loss Calibration via ATR Buffer
- Rather than placing the Stop Loss exactly at the high/low of the wick, add a dynamic volatility cushion based on the Average True Range (14-period 15m ATR):

$$\text{SL}_{\text{Long}} = \text{Wick Low} - (1.5 \times \text{ATR}_{15m})$$
$$\text{SL}_{\text{Short}} = \text{Wick High} + (1.5 \times \text{ATR}_{15m})$$

- **Position Size Adjustment:** Widening the Stop Loss via $1.5\times$ ATR proportionally REDUCES contract quantity, guaranteeing that total monetary risk in USDT remains exactly constant ($1.50 USDT standard loss).
