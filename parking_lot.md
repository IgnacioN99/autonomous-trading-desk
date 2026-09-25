# 🅿️ Parking Lot — Registro Inmutable de Estrategias e Ideas Diferidas

Este archivo es el repositorio formal de ideas cuantitativas, arquitecturas e hipótesis que están **fuera del alcance inmediato**, pero que deben preservarse intactas sin generar ruido ni context rot en las sesiones operativas diarias.

---

## 💡 1. Estrategias Cuantitativas Diferidas

### [PL-01] Arbitraje de Tasa de Financiación Tripartita (Funding Harvest Multi-Exchange)
* **Fecha:** 2026-09-23 | **Estado:** `DIFERIDA`
* **Hipótesis:** Long Spot (o Perp en exchange con funding negativo) vs Short Perp en Binance (funding positivo extremo). Capturar el yield pasivo cada 8 horas sin exposición direccional ($\Delta = 0$).
* **Requisitos:** Integración con segundo exchange (Bybit / Hyperliquid / OKX) y sincronización atómica de órdenes entre APIs.
* **Gatillo de activación:** Cuando la tasa de financiamiento anualizada supere el 25% APR durante más de 3 días consecutivos.

### [PL-02] Stat-Arb Cointegrado con Filtro de Kalman Adaptativo
* **Fecha:** 2026-09-23 | **Estado:** `EN EVALUACIÓN`
* **Hipótesis:** Sustituir la regresión OLS estática de beta ($\beta_{A/B}$) por un Filtro de Kalman de espacio de estados que actualice el ratio de cobertura en tiempo real barra a barra para absorber cambios estructurales de volatilidad.
* **Requisitos:** Implementar `pykalman` o filtro bayesiano propio en `scripts/quant_risk_engine.py`.

### [PL-03] Microestructura L2: Order Flow Toxicity (VPIN - Volume-Synchronized Probability of Toxicity)
* **Fecha:** 2026-09-23 | **Estado:** `DIFERIDA`
* **Hipótesis:** Calcular el VPIN en bloques de volumen constante para anticipar quiebres de soportes por flujo institucional tóxico antes de que aparezcan en velas de 15m.

---

## 🛠️ 2. Infraestructura y Automatización

### [PL-04] Feed Directo WebSocket para Trailing Stop de Cero Latencia
* **Fecha:** 2026-09-23 | **Estado:** `DIFERIDA`
* **Hipótesis:** Reemplazar el sondeo REST de precios por un listener WebSocket directo a `wss://fstream.binance.com/ws/!miniTicker@arr` para actualizar trailing stops en sub-100ms.
* **Riesgo:** Requiere mantener un proceso demonio persistente en background.

### [PL-05] Notificaciones Push de Ejecución y Alertas (Telegram / Discord Webhook)
* **Fecha:** 2026-09-23 | **Estado:** `LISTO PARA DESARROLLO`
* **Hipótesis:** Disparar un mensaje breve cada vez que el Fast-Track ejecute una orden, se cobre un TP1 o el Night Cutoff Loop blinde a Break-Even.

---

## 📈 3. Gestión de Capital y Reglas de Escala

### [PL-06] Transición a Mainnet con Escalado Gradual de Riesgo
* **Fecha:** 2026-09-23 | **Estado:** `DIFERIDA`
* **Criterio de Graduación:** Requiere acumular al menos 50 operaciones auditadas en Testnet con Sharpe Ratio $> 1.4$, Max Drawdown $< 5\%$ y cero fallos de Stop Loss huérfano.

---

*Regla: Toda nueva idea se agrega al final sin reescribir las anteriores. Cuando una idea se implementa, se marca como `GRADUADA` citando el PR o commit correspondiente.*
