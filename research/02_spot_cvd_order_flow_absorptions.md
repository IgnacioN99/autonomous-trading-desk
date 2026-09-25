# Microestructura del Flujo de Órdenes: Spot CVD vs Perpetual CVD

## 1. Definición y Mecánica del Cumulative Volume Delta (CVD)
El Volume Delta representa la diferencia neta entre el volumen ejecutado al precio de demanda (compras agresivas a mercado) y el volumen ejecutado al precio de oferta (ventas agresivas a mercado):

Delta = Volumen_Ask - Volumen_Bid
CVD_t = Sumatorio(Delta_i) desde i=0 hasta t

## 2. La Disparidad Spot CVD vs Perpetual CVD (Detección de Trampas)
En Binance y los principales exchanges, los mercados Spot y Futuros Perpetuos operan con dinámicas de liquidez radicalmente distintas:
- Spot CVD: Refleja compras y ventas de inversores con capital real 1:1, sin apalancamiento forzado ni liquidaciones sintéticas. Representa el dinero institucional genuino.
- Perpetual CVD: Refleja la agresividad de operadores apalancados (retail e intradía), fuertemente influenciado por cascadas de liquidación y stop hunts.

### Patrones de Divergencia Críticos:
1. Trampa de Apalancamiento (Bull Trap / Stop Hunt):
   - El precio sube y el Perpetual CVD se dispara verticalmente.
   - El Spot CVD permanece plano o diverge a la baja (ventas pasivas al contado).
   - Diagnóstico: Subida frágil financiada con deuda minorista. Los market makers absorberán la liquidez y provocarán un flash dump hacia el soporte previo.
2. Absorción Institucional en Soporte (Accumulation Footprint):
   - El precio cae hacia un nivel de soporte y se lateraliza.
   - El Perpetual CVD cae en pánico (ventas retail), pero el Spot CVD comienza a subir o el precio deja de marcar mínimos más bajos.
   - Diagnóstico: Compradores pasivos al contado están absorbiendo toda la oferta agresiva. Configuración de compra de alta probabilidad.
