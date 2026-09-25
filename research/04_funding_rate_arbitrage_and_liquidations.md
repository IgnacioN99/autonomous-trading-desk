# Arbitraje de Funding Rate y Mapas de Calor de Liquidaciones

## 1. Estrategia Cash-and-Carry (Delta Neutral)
La estrategia con mayor Ratio de Sharpe documentado en futuros de criptomonedas (Sharpe 4.84, Drawdown < 2%):
- Comprar el activo al contado (Spot BTC/ETH).
- Abrir una posición corta equivalente (1x Short) en Futuros Perpetuos.
- Cobro sistemático de la Tasa de Financiamiento (Funding Rate) cada 8 horas cuando los perpetuos cotizan con prima sobre spot.
- Rendimiento: 15% a 55% APR libre de riesgo direccional.

## 2. Dinámica de Clústeres de Liquidación (Liquidation Heatmaps)
Los niveles de precio donde se concentran volúmenes masivos de liquidaciones estimadas actúan como imanes de liquidez:
1. Fase de Atracción: El precio es empujado hacia los grupos densos de liquidación para ejecutar las órdenes forzadas.
2. Fase de Agotamiento: Una vez absorbido el clúster de liquidaciones, si no entra nuevo volumen spot, ocurre una reversión brusca en sentido contrario (Mean Reversion).
3. Regla Operativa: No perseguir rupturas que acaban de atravesar un clúster mayor de liquidación; buscar la reversión a la EMA 20 tras la absorción.
