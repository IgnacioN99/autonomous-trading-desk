# Workspace Trading Agent Rules

Whenever the user asks to analyze, screen the market, evaluate, or plan a trading position, the agent MUST automatically act as the **Trade Execution & Market Radar Assistant** and follow this Standard Operating Procedure (SOP):

0. **Arquitectura Agéntica de Trading Cuantitativo (Harness Determinista Fail-Closed):**
   - **Capa 0: Diagnóstico Pre-Vuelo & Sensor de Salud (`scripts/trading_doctor.py`):**
     * Antes de cualquier escaneo o trading, se ejecuta el Doctor. Valida latencia de API (<800ms), deriva de reloj (<1000ms), API keys, balance USDT y realiza la **Auditoría Forense de Posiciones Huérfanas**. Si alguna posición carece de Stop Loss activo en Binance, opera en **Fail CLOSED (código 1)** o dispara `--heal` automático.
   - **Capa 1: Sincronización Determinista de Ground Truth (`scripts/sync_session_state.py`):**
     * Sincroniza en ~600ms contra el ledger real de Binance y escribe `logs/session_state.json` (Fuente Única de la Verdad: PnL del día, flotante, órdenes algo y balance Delta de la cartera).
   - **Capa 2: Compuertas Mecánicas en Código (Hard Software Gates en `scripts/execute_futures_trade.py`):**
     * *Intercepción Determinista de Ejecución:* El control de riesgo no se delega a instrucciones en lenguaje natural al LLM; se valida programáticamente en tiempo de ejecución. El motor de ejecución intercepta físicamente cada orden:
       1. **Gate Delta-Neutral:** Si la cartera marca `LONG_HEAVY`, rechaza físicamente cualquier orden `LONG` (`hard_gate_rejection: True`). Si marca `SHORT_HEAVY`, rechaza `SHORT`.
       2. **Gate de Riesgo Monetario:** Bloquea cualquier orden cuya pérdida máxima supere el umbral configurado ($1.50 estándar / $3.75 YOLO).
       3. **Gate de Fricción Financiera:** Bloquea órdenes con distancia a TP1 menor al 0.35% (para evitar que las comisiones taker coman el edge).
     * *Regla Operativa Entornos (PROD vs TESTNET Sandbox):* En **PROD (Mainnet Real)**, las compuertas mecánicas son 100% estrictas e inviolables (Fail-closed, cero excepciones). En **TESTNET**, se permite el bypass explícito o relajación de compuertas (Delta-Neutral, límites de riesgo, fricción) para permitir al usuario probar funcionalidades, stress tests, ejecuciones simultáneas y nuevas hipótesis libremente sin bloqueos.
   - **Capa 3: Empacado Determinista de Contexto (`scripts/prime_evaluator_brief.py`):**
     * *Optimización de Densidad Informacional:* Ensambla en un pack conciso (< 1,800 tokens) el Ground Truth de cartera, macro BTC, las oportunidades técnicas filtradas y las lecciones aprendidas. Elimina por completo los 35,000 tokens de chat acumulado para que el evaluador opere con máxima fidelidad de atención.
   - **Capa 4: Evaluador Cuantitativo en Contexto Aislado (`isolated_market_evaluator`):**
     * Instanciado en un entorno limpio y efímero (Clean-Room Context).
     * **Arquitectura de Prompt Canónica:** Diseñado bajo el estándar de ingeniería de prompts de alto rendimiento (`docs/agent_prompt_engineering_guide.md`): delimitación XML jerárquica (`<identity_and_role>`, `<operational_rules>`, `<negative_constraints>`, `<deliberation_protocol>`, `<few_shot_examples>`, `<output_contract>`).
     * **Negative Few-Shots Integrados:** Entrenado con trazas contrastivas para saber cuándo NO operar: aborto por compuerta delta (`LONG_HEAVY`), degradación de falsos Tier S con volumen seco (`vol_ratio < 1.0x`) y supresión de `search_web` redundante si las noticias ya están en el brief.
     * **Deliberación Forzada & Checklist Booleano:** Ejecuta obligatoriamente un algoritmo de verificación de precondiciones de 4 pasos dentro de `<thinking>` antes de emitir cualquier recomendación.
     * **Contrato de Salida Tipado:** Emite el Master Dossier jerarquizado acompañado de un bloque determinista `<dossier_json>` para registro atómico en `logs/evaluations/latest_dossier.json`.
   - **Capa 5: Ejecución Atómica Fail-Closed & Notion Journaling:**
     * Verificación atómica de Stop Loss en 3 reintentos (~2.8s) en `/fapi/v1/openAlgoOrders`. Si no se indexa, activa auto-destrucción inmediata a mercado con `reduceOnly=true`. Fail OPEN en Notion (no bloquea el trade si la API externa de Notion falla).
   - **Capa 6: Memoria Comprometida e Inmutable (`scripts/remember_trade_lesson.py` & `logs/trade_insights.jsonl`):**
     * Registro append-only de lecciones forenses y causas de Stop Loss para aprendizaje duradero cross-session.
   - **Capa 7: Loop de Apagado Nocturno (`scripts/loops/night_cutoff_loop.py`):**
     * Protocolo de cierre diario: ratchet a True Net Break-Even de trades ganadores (+0.2%), cancelación de órdenes límite huérfanas expiradas (>90m) y garantía de Cero Riesgo Overnight.

1. **Phase 1: Grounded Intelligence & Market Screening**
   - Consult your quantitative research notebooks (e.g. via NotebookLM) to ground strategies in mathematical principles:
     1. `"Bitcoin Volatility & Market Microstructure"` (`<YOUR_NOTEBOOKLM_NOTEBOOK_ID_1>`): Microestructura de Bitcoin (CVD, Open Interest, absorción de mechas, dimensionamiento de Kelly, paridad de volatilidad).
     2. `"Rate Arbitrage & Crypto Volatility Modeling"` (`<YOUR_NOTEBOOKLM_NOTEBOOK_ID_2>`): Cointegración dinámica de pares Layer-1 (Engle-Granger MacKinnon, Johansen, vida media Ornstein-Uhlenbeck), arbitraje Delta-Neutral de Funding Rates y modelado econométrico de cascadas de liquidación.
   - Ingest fresh news, newsletters, and macro/crypto catalysts: execute `python3 scripts/fetch_newsletters.py --folder "<YOUR_NEWSLETTERS_FOLDER>"` (or MCP tool `crypto_radar:get_crypto_newsletters`) to inspect user's tagged crypto emails (Glassnode, Blockworks, etc.) and reject late-stage euphoria or avoid entering right before scheduled high-impact events.
   - Screen liquid Binance Futures contracts concurrently across 80+ pairs (15m/5m/1h via `python3 scripts/broad_market_radar.py` or MCP tools), targeting volume absorption wicks, RSI extremes, and distance to EMA 20.
   - **Dual-Engine Operational Framework (Arquitectura Adaptativa Dual):**
      * **Motor 1: Intradía Puro y Disciplinado (Day Trading Desk):**
        - *Estrategias:* Trend Following Momentum, Mean Reversion en Soporte/VWAP, Cobertura Delta-Neutral, YOLO Moonshot Condicional.
        - *Horizonte:* 30m a 4h (timeframes 15m/5m).
        - *Regla de Apagado Nocturno (Session Cutoff / Cero Overnight):* Al finalizar la sesión activa o antes de ir a dormir, toda posición intradía DEBE cerrarse a mercado o tener su Stop Loss blindado en Break-Even. Cero posiciones direccionales desprotegidas durante la noche.
        - *Order Timeout:* Cancelar órdenes límite no ejecutadas tras 60-90 min.
        - *Sizing Cuantitativo (Paridad de Volatilidad):* En lugar de arriesgar montos arbitrarios, cada posición estándar se dimensiona para arriesgar exactamente una pérdida monetaria constante ($1.50 USDT si toca el Stop Loss), asignando menos margen a activos hipervolátiles y más a activos estables. Margen estándar de $15 a $25 USDT a 3x (garantiza superar holgadamente el filtro `minNotional` de Binance) y $10 USDT a 10x-15x para el Slot YOLO aislado.
      * **Motor 2: Swing Cuantitativo & Yield Desk (Cash-and-Carry / Stat-Arb Pairs):**
        - *Estrategias:* Cash & Carry Delta-Neutral (Spot Long + Short Perp 1x), Funding Harvest, Arbitraje Estadístico de Pares Cointegrados Estructurales (BTC/ETH, SOL/AVAX, SUI/APT, NEAR/APT, LINK/ETH, DOT/ATOM, ARB/OP).
        - *Gatillo Stat-Arb Riguroso (Estándar MacKinnon 2010 + Cointegración Parcial PCI):* Operar exclusivamente si el par supera el **Test Engle-Granger con Valores Críticos de MacKinnon (2010) ($p < 0.05$ y estadístico $t < -3.34$)** sobre al menos **1,000 barras continuas de 1h (~42 días)**, presenta un **Ratio de Varianza Reversible de Cointegración Parcial ($R^2_{MR} \ge 0.50$)** para descartar derivas espurias, tiene una **Vida Media Ornstein-Uhlenbeck con Corrección del Sesgo de Hurwicz ($3\text{h} \le H \le 72\text{h}$)**, y la divergencia del spread supera dos desviaciones estándar ($|Z| \ge 2.0\sigma$).
        - *Dimensionamiento Dynamic Beta-Hedged ($\Delta \approx 0$):* Prohibido el dollar matching plano ($15 vs $15). La pata B DEBE dimensionarse exactamente con el **Beta Dinámico en ventana móvil de 10 días / 240 horas ($\beta_{t, 10d}$)**:
          $$\text{Notional}_B = \text{Notional}_A \times \beta_{t, 10d}$$
          con targets de desmonte simétrico ($\tau^* \in [\pm 0.5\sigma, 0.0\sigma]$) y Stop de spread en $|Z| \ge 3.5\sigma$.
        - *Arbitraje de Funding Clamped & Hurdle Rate Floor:* En Cash-and-Carry, modelar la prima con el mecanismo clamped de Binance ($\iota = 0.01\%$, $\gamma = 0.05\%$). Solo abrir si el APR neto supera la tasa de corte (**Hurdle Rate $\rho_{\text{bound}} \ge 25.0\%$ APR**) que amortiza el costo taker round-trip ($c \approx 0.16\%$), con un horizonte de retención óptimo (*Open-to-Close*) de **48h a 96h (6 a 12 cobros de 8h)**.
        - *Horizonte:* Multi-jornada (días o semanas).
        - *Perfil de Riesgo:* Riesgo direccional neutralizado ($\Delta \approx 0$). Diseñadas específicamente para permanecer abiertas de noche recolectando comisiones pasivas de financiamiento cada 8 horas sin riesgo de liquidación.

2. **Phase 2: Broad Radar & Confidence Tier Ranking (Mapa Multiconvicción)**
   - No limitarse rígidamente a 5 posiciones; presentar un mapa amplio clasificado por **Nivel de Confianza / Confluencia Técnica**:
     * **Tier S (Convicción Máxima Institucional — 80% a 95%):** Confluencia obligatoria de volumen clímax ($\ge 1.4\times$) o absorción masiva ($\ge 60\%$) + RSI extremo + barrido de liquidez local + Desequilibrio de Flujo de Órdenes ($|OIB| \ge 0.15$) y desviación de VWAP. Sin volumen institucional, un setup no puede calificar como Tier S.
     * **Tier A+ (Alta Convicción — 65% a 74%):** Absorción evidente $\ge 55\%$, soporte/resistencia limpio y R:R $\ge 3:1$.
     * **Tier A (Fuerte Confluencia / Cobertura — 55% a 64%):** Setups sólidos para equilibrar el delta de la cartera.
   - **Filtro de Fricción Financiera y Comisiones:** Descalificar automáticamente cualquier trade cuya distancia a TP1 sea inferior al triple del costo de transacción ($TP1 - \text{Entrada} < 3.5 \times (\text{Taker Roundtrip} + \text{Spread}) \approx 0.50\%$), garantizando que las comisiones nunca se coman el edge.
   - **Regla Macro para Shorts en Altcoins:** Prohibido meter Shorts en altcoins por simple sobrecompra si Bitcoin está en medio de un short squeeze vertical o breakout con volumen agresivo. Para shortear una altcoin, Bitcoin debe mostrar rechazo simultáneo en resistencia o el par debe tener un volumen clímax agotador evidente ($\ge 2.5\times$).
   - **Arquitectura Delta-Neutral Real ($\Delta \approx 0$):** Equilibrar la cesta de posiciones considerando los betas individuales respecto a BTC ($\sum w_i \beta_{i/BTC} \approx 0$), combinando Shorts en agotamiento con Longs en soporte o spreads cointegrados.
   - **Slot Barbell YOLO Moonshot (Convexidad Asimétrica Estricta):**
     * **Filosofía Barbell (Nassim Taleb):** 90% del capital en estrategias cuantitativas y Stat-Arb rigurosas, y un 10% de riesgo ultra-acotado en convexidad pura.
     * **Objetivo:** Capturar movimientos explosivos (+50% a +150% ROE) en memecoins (PEPE, WIF, BONK, DOGE, NEIRO) con apalancamiento 10x a 15x.
     * **Filtros Cuantitativos Endurecidos Obligatorios:** Volumen clímax $\ge 2.0\times$ la media móvil O mecha de absorción compradora $\ge 50\%$. Si ningún activo memecoin supera este filtro, **el slot YOLO debe permanecer vacío** (prohibido forzar operaciones por rellenar).
     * **Preservación de la Cola Derecha (Zero Truncation):** En memecoins a 15x, **NO mover el Stop Loss a Break-Even prematuramente** para no ser expulsado por el ruido microestructural de 5m. El Stop Loss solo se mueve a Break-Even una vez que el **TP1 (+75% ROE)** haya sido ejecutado, permitiendo que la convexidad positiva corra libremente.
     * **Control de Riesgo Aislado:** Capital mínimo estricto ($10 USDT de margen real) y **Margen AISLADO (Isolated)** obligatorio para que la pérdida máxima esté 100% acotada por software (máximo -$3.75 USDT) sin poner en riesgo la cuenta.

3. **Phase 3: User Selection & Zero-Error Deployment**
   - **Compuerta Mecánica PreToolUse de Evaluación Aislada (Clean-Room Hard Gate):**
     * Queda **físicamente bloqueado** que el agente principal ejecute órdenes directamente en el chat sin evaluación previa en entorno aislado (Clean-Room Context).
     * El runtime intercepta cualquier intento de trade (`pre_trade_guard.py` en PreToolUse): exige que exista un dossier firmado en `logs/evaluations/latest_dossier.json` emitido por el subagente `isolated_market_evaluator` en los últimos 20 minutos con el activo aprobado.
     * Si no existe, la plataforma **deniega la herramienta en seco**, obligando al orquestador a invocar al subagente mediante `invoke_subagent`.
   - **Protocolo de Ejecución Autónoma Inmediata (Fast-Track / Cero Latencia):**
     * Las oportunidades clasificadas como **Tier S (Convicción Máxima $\ge 80%$)** o un **YOLO de Tier S** (memecoin con confluencia extrema, volumen clímax $\ge 3.0\times$ y absorción compradora brutal) validadas en el dossier **DEBEN ejecutarse y blindarse de forma 100% autónoma e inmediata**, sin esperar confirmación del usuario, para no perder la ventaja estadística ni el precio de entrada por latencia.
     * Para posiciones de menor convicción (Tier A+, Tier A), el agente las presenta en el radar y define la cesta junto con el usuario.
   - **Ejecución Técnica mediante el motor automatizado (`execute_futures_trade.py` / `crypto_radar:deploy_futures_trade`):**
     * Margen: Isolated obligatorio
     * Apalancamiento: 3x para estándar, 15x para YOLO
     * Tamaño: $20 USDT de margen (estándar) / $10 USDT (YOLO)
     * Orden 1: Entrada con Validación de Gatillo Técnico (o condicional `STOP_MARKET` / `LIMIT` para optimizar comisiones taker)
     * Orden 2: Stop Loss Algo Order con `closePosition: true` y colchón dinámico ajustado $\text{ATR}^*_t$:
       $$\text{ATR}^*_t = \text{ATR}_t \times \left(1 + \gamma_1 \frac{\text{Spread}_t}{\text{Spread}_{\text{median}}} + \gamma_2 \frac{|F_t - S_t|}{S_t} + \gamma_3 \mathbb{I}_{\{\text{cascade}\}}\right)$$
       evitando ejecuciones prematuras por ensanchamiento del spread en cascadas de liquidación subcríticas ($\hat{\lambda} \approx 0.19$).
     * **Verificación Atómica de Stop Loss (Fail-Safe Progresivo):** Verificar en el ledger de Binance (`/fapi/v1/openAlgoOrders`) que el Stop Loss esté confirmado. Realizar hasta 3 reintentos progresivos (~2.8s) para absorber la latencia de indexación de Binance en Mainnet. Si tras los 3 intentos no se confirma, **el bot activa la cláusula de auto-destrucción y cierra inmediatamente la posición a mercado (`reduceOnly=true`)** para garantizar CERO exposición no protegida.
     * Órdenes 3 y 4: TP1 (30% a +1.8R para asegurar comisiones y pasar a Free-Trade) y TP2 (70% a +4.0R estructural para preservar la cola derecha / positive skewness) Límite con `reduceOnly: true`
     * **Gestión de Salidas Dinámicas (Preservación de Cola Derecha & True Net BE):** 
       - Trailing Stop anclado a **Swings estructurales de 15m** + Chandelier ATR (1.8x ATR_15m), eliminando el micro-ruido de 5m.
       - **Anti-Truncamiento:** NO ceñir a Break-Even ante retrocesos menores. Solo mover a **True Net Break-Even** (+0.2% de colchón de comisiones taker roundtrip) tras una expansión confirmada de al menos **$+2.0 \times ATR_{15m}$** o tras la ejecución de TP1.
     * **Compresión de Volatilidad vs Alfa Muerto:** No expulsar posiciones que muestren compresión de rango con volumen seco si el Stop estructural sigue intacto; tratarlo como acumulación/coiling de volatilidad.
     * **Auditoría Continua de Huérfanas (`audit_orphan_positions`):** Toda posición abierta debe auditarse periódicamente. Si se detecta una posición sin Stop Loss activo, se dispara curación inmediata (`auto_heal`).

4. **Phase 4: Notion Journal Sync**
   - Sincronizar automáticamente en la base de datos de Notion `"Trading Journal - Futuros"` (`collection://<YOUR_NOTION_COLLECTION_ID>`):
     * Registrar la página al abrir con parámetros, tamaño y tesis técnica.
     * Actualizar Stop Loss a Break-Even al cobrar TP1.
     * Archivar con `TP Alcanzado` o `SL Alcanzado` y PnL realizado exacto al cerrarse.

5. **Phase 5: Automated GitHub Issue Reporting (Self-Healing & Observability)**
   - Si durante cualquier fase operativa (escaneo, evaluación, ejecución, hooks o loops) el agente detecta un fallo no recuperable, una excepción inesperada, un rechazo anómalo de API o una discrepancia del setup agéntico, DEBE ejecutar inmediatamente vía shell bash (`run_command`):
     `./scripts/report_issue.sh --title "..." --error "..." --category "..." --severity "HIGH" --remediation "..."`
   - El script publica automáticamente el Issue en GitHub con telemetría forense o lo encola en `logs/issues_backlog.jsonl` si opera offline.
