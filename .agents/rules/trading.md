---
trigger: always_on
---

# Trading Desk Safety Invariants (summary; full rules in AGENTS.md)

- Fail-closed: without active pre-trade hooks, live order execution is PROHIBITED. The agent cannot run `/hooks`: hooks count as active only if `logs/hook_heartbeat.json` was updated by `pre_trade_guard.py` in this session or `python3 scripts/trading_doctor.py` reports the guard OK.
- Never call Binance write tools directly (`futures_usds.newOrder`, `spot.newOrder`, etc.). Open and manage trades only via `scripts/execute_futures_trade.py` (`--positions`, `--move-breakeven --symbol`, `--close-position`, `--audit-orphans`, `--auto-heal`). The `crypto_radar` MCP server is retired and denied by the hook.
- Scans are read-only CLI scripts with `--json` (`.agents/skills/market-radar/SKILL.md`); trailing stops, dead alpha and orphan audits run in `scripts/loops/position_guardian_loop.py`. Third-party Binance skills may be installed but are not part of the flow: never use them to place orders, move funds or sign API requests.
- Mandatory evaluation before every trade: `python3 scripts/prime_evaluator_brief.py` → `invoke_subagent` (TypeName `isolated_market_evaluator`, defined in `.agents/agents/isolated_market_evaluator/agent.md`) → wait for its message → `python3 scripts/record_evaluation.py --from-subagent <conversationId>`. Never write the dossier by hand or use `define_subagent`; in PROD `--symbols`/`--json-file` are rejected.
- The dossier (`logs/evaluations/latest_dossier.json`, <20 min) must approve symbol and direction. Tier A/A+ (`requires_user_confirmation: true`) require explicit user confirmation in chat.
- Risk and leverage come from `config/user_profile.json` (`risk_pct_equity`, `leverage_standard`, `leverage_yolo`, ceiling `leverage_ceiling`, default 15x); never fixed amounts. `BINANCE_AUTH_MODE=MCP` (agentic sub-account; Binance caps it at 5x, error -4421, the executor clamps) or `KEYS` (futures-only HMAC API, no withdrawals, IP-restricted).
- Every position must have a verified Stop Loss; if it is not confirmed, close immediately with `reduceOnly`. Risk-reducing actions (closing, moving SL to BE) are never blocked.
- agy must be launched from the repo root in a POSIX shell (Linux/macOS/WSL); native agy on Windows (hooks via `cmd /c`) is not supported. `.agents/hooks.json` uses paths relative to `.agents/` (the hooks' cwd), never absolute ones.
- On unrecoverable failures: `./scripts/report_issue.sh --title "..." --error "..." --category "..." --severity "HIGH" --remediation "..."`.
