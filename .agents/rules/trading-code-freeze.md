---
trigger: always_on
---

# Code Freeze During Trading Operations

Applies whenever the session does trading work: market scans or analysis, evaluator runs, opening/managing/closing positions, publishing or reporting positions, journaling.

- **The repository is read-only for you during trading work.** Never create, edit, move or delete repo files (`scripts/`, `tests/`, `.agents/`, `.claude/`, `AGENTS.md`, `CLAUDE.md`, `README.md`, `docs/`, `config/*.example`, hooks), never run `git commit/checkout/reset/stash/push/merge`, never install or upgrade dependencies, and never write ad-hoc scripts that replicate or patch desk logic. Runtime outputs written by the desk scripts themselves (`logs/`) are expected.
- **The code is tested and production-ready.** If a script fails, a gate behaves unexpectedly, output is malformed, or a hook denial looks like a bug: stop that step (fail closed — no retries with variations, no workarounds), keep open positions protected using only the sanctioned risk-reducing CLI (`scripts/execute_futures_trade.py --close-position | --move-breakeven --symbol X | --auto-heal`, `scripts/loops/position_guardian_loop.py --once`), and open a GitHub issue:
  `./scripts/report_issue.sh --title "<script>: <short failure>" --error "<exact command, exit code and error>" --category tool_error|risk_gate|infra|agent_failure --severity CRITICAL|HIGH|MEDIUM|LOW --remediation "<suspected cause / suggested fix>"`
  (offline, it queues to `logs/issues_backlog.jsonl`). Give the user the issue link and continue only with steps the failure does not affect.
- **Severity:** CRITICAL = a position is unprotected or its stop cannot be verified; HIGH = execution or a gate is blocked; MEDIUM = scan/analysis degraded; LOW = cosmetic.
- **Fixes are a separate development task:** only when the user explicitly asks to fix an issue, on a feature branch with tests and a PR — never mixed into a trading session.
