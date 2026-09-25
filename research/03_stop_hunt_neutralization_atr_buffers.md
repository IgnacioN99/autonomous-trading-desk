# Neutralización de Barridos de Liquidez (Stop Hunts) y Parámetros Cuantitativos

## 1. Anatomía de un Liquidity Sweep en Criptomonedas
Los mercados de futuros cripto son altamente asimétricos. Los creadores de mercado y algoritmos de alta frecuencia rastrean las agrupaciones de órdenes de Stop Loss ubicadas milimétricamente por debajo de soportes visibles o por encima de resistencias.

### Por qué fallan las entradas inmediatas:
Cuando un trader entra exactamente en la mecha de rechazo o al cierre de la vela de 15m, queda vulnerable al segundo 'barrido de confirmación' (segunda mecha que penetra unos pips adicionales para limpiar los stops tardíos).

## 2. El Filtro del Gatillo en la Vela Siguiente (Next-Candle Confirmation Trigger)
Basado en los estudios de Thomas Bulkowski y la microestructura de order flow:
1. Regla de Entrada: NUNCA entrar a mercado al cierre de la vela que forma la mecha o el patrón morfológico.
2. Gatillo Obligatorio:
   - Para LONG: La siguiente vela debe romper el máximo absoluto de la vela del patrón (High_trigger = High_patron + 0.05%).
   - Para SHORT: La siguiente vela debe romper el mínimo absoluto de la vela del patrón (Low_trigger = Low_patron - 0.05%).
   - Si la siguiente vela no supera el extremo y retrocede, la orden se descarta inmediatamente, evitando el stop hunt.

## 3. Calibración Dinámica del Stop Loss con Buffer ATR
- En lugar de colocar el Stop Loss exactamente en el mínimo/máximo de la mecha, se debe añadir un colchón de volatilidad basado en el Average True Range (ATR de 14 periodos en 15m):

SL_Long = Minimo_Mecha - (1.5 * ATR_15m)
SL_Short = Maximo_Mecha + (1.5 * ATR_15m)

- Ajuste de Tamaño: Al ampliar el Stop Loss mediante 1.5x ATR, el número de contratos o tokens se REDUCE proporcionalmente mediante la fórmula de Kelly, garantizando que el riesgo monetario en dólares (USDT) siga siendo constante ( -  USDT).
