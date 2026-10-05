---
trigger: always_on
---

# Trading Desk Safety Invariants (resumen; reglas completas en AGENTS.md)

- Fail-closed: sin hooks pre-trade activos está PROHIBIDA la ejecución de órdenes reales. El agente no puede ejecutar `/hooks`: los hooks cuentan como activos solo si `logs/hook_heartbeat.json` fue actualizado por `pre_trade_guard.py` en esta sesión o `python3 scripts/trading_doctor.py` reporta el guard OK.
- Nunca llamar directamente a tools de escritura de Binance (`futures_usds.newOrder`, `spot.newOrder`, etc.). Abrir trades solo vía `crypto_radar:deploy_futures_trade` o `scripts/execute_futures_trade.py`.
- Evaluación obligatoria antes de cada trade: `python3 scripts/prime_evaluator_brief.py` → `invoke_subagent` (TypeName `isolated_market_evaluator`, definido en `.agents/agents/isolated_market_evaluator/agent.md`) → esperar su mensaje → `python3 scripts/record_evaluation.py --from-subagent <conversationId>`. Nunca escribir el dossier a mano ni usar `define_subagent`; en PROD `--symbols`/`--json-file` se rechazan.
- El dossier (`logs/evaluations/latest_dossier.json`, <20 min) debe aprobar símbolo y dirección. Tier A/A+ (`requires_user_confirmation: true`) requieren confirmación explícita del usuario en el chat.
- Riesgo y apalancamiento salen de `config/user_profile.json` (`risk_pct_equity`, `leverage_standard`, `leverage_yolo`, techo `leverage_ceiling`, default 15x); nunca montos fijos. `BINANCE_AUTH_MODE=MCP` (sub-cuenta agéntica, Binance limita a 5x, error -4421, el executor recorta) o `KEYS` (API HMAC solo futuros, sin retiros, con IP restringida).
- Toda posición debe tener Stop Loss verificado; si no se confirma, cierre inmediato `reduceOnly`. Acciones que reducen riesgo (cerrar, mover SL a BE) nunca se bloquean.
- agy debe lanzarse desde la raíz del repo en un shell POSIX (Linux/macOS/WSL); agy nativo en Windows (hooks vía `cmd /c`) no está soportado. `.agents/hooks.json` usa rutas relativas a `.agents/` (cwd de los hooks), nunca absolutas.
- Ante fallos irrecuperables: `./scripts/report_issue.sh --title "..." --error "..." --category "..." --severity "HIGH" --remediation "..."`.
