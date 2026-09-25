# Criterio de Kelly y Gestión Cuantitativa de Riesgo en Derivados Cripto

## 1. Fundamentos Matemáticos del Criterio de Kelly
El Criterio de Kelly determina la fracción óptima del capital (f*) a arriesgar en una serie de operaciones para maximizar la tasa de crecimiento logarítmico del capital a largo plazo:

f* = (b * p - q) / b = p - (q / b)

Donde:
- p: Probabilidad empírica de acierto (Win Rate).
- q: Probabilidad de pérdida (1 - p).
- b: Ratio de beneficio a pérdida (R:R o Ganancia promedio / Pérdida promedio).

### El Peligro del Full Kelly en Criptoactivos
En mercados con alta asimetría de colas (fat tails), deslizamiento (slippage) y volatilidad extrema como las criptomonedas, el Full Kelly conduce matemáticamente a drawdowns inaceptables (>50%) o a la ruina por sobreestimación del win rate.

Por tanto, el estándar cuantitativo utiliza Fractional Kelly:
- Half-Kelly (f* / 2): Captura el 75% del crecimiento óptimo reduciendo la varianza del balance en un 50%.
- Quarter-Kelly (f* / 4): Estándar obligatorio para cuentas micro (<,000 USD) en Binance Futures, limitando el riesgo real por operación al 1.0% - 2.5% del capital total.

## 2. Dimensionamiento de Posición en Cuentas Micro (Binance Futures)
Para un capital micro (C = 00 - 00 USDT):
- Riesgo Máximo por Operación (R_trade): 2.0% del capital de la cuenta ( a 0 USDT de pérdida máxima).
- Fórmula de Tamaño de Posición (Nocional en Contratos):
  Nocional (USDT) = R_trade / Distancia al SL (%)
  Margen Requerido (USDT) = Nocional (USDT) / Apalancamiento

### Regla de Oro del Margen Aislado (Isolated Margin)
- Toda operación intradía y en especial slots de alta volatilidad (YOLO/Memecoins a 10x-20x) DEBE ejecutarse en modo ISOLATED.
- El saldo total de la cuenta jamás debe respaldar una posición abierta. Si ocurre una anomalía o gap down, la pérdida queda estrictamente acotada al margen asignado.

## 3. Control de Ruina y Circuit Breakers Diarios
1. Regla de los Tres Strikes: 3 pérdidas consecutivas intradía activan el apagado automático del terminal durante 12 horas.
2. Límite Diario de Pérdida (Max Daily Drawdown): 4.0% - 5.0% del balance de la cuenta. Si se alcanza, se cancelan todas las órdenes límite activas y se cierran exposiciones.
3. Velocidad de Reciclaje de Capital: Cancelación forzosa de órdenes no ejecutadas tras 60-90 minutos para liberar margen.
