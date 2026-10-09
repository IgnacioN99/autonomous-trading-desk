@AGENTS.md
@.agents/rules/trading.md
@.agents/rules/trading-code-freeze.md

# Claude Code

AGENTS.md and the always-on rules in `.agents/rules/` (imported above) are the single source of truth
and apply to Claude Code exactly as they apply to Google Antigravity (agy). This section only maps the
agy mechanics to Claude Code.

## Where things live

| What | agy (source of truth) | Claude Code |
|---|---|---|
| Rules | `AGENTS.md`, `.agents/rules/*.md` | this file (imports them) |
| Subagents | `.agents/agents/<name>/agent.md` | `.claude/agents/<name>.md` (generated) |
| Skills | `.agents/skills/{market-radar,trade-execution-planner,pr-review,issue-orchestrator}/SKILL.md` | `.claude/skills/<skill>/SKILL.md` (generated) |
| Hooks | `.agents/hooks.json` | `.claude/settings.json` (+ `.claude/settings.local.json` on Windows) |
| MCP servers | `.agents/mcp_config.json` | `.mcp.json` |

- Never edit `.claude/agents/` or `.claude/skills/` by hand: edit `.agents/*`, then run
  `python3 scripts/dev/sync_claude_assets.py` (tests run it with `--check` and fail on stale files).
  New or renamed subagents only appear as `subagent_type` values after restarting the Claude Code session.
- Tool names: `invoke_subagent` = Agent tool (`subagent_type`), `send_message` = the subagent's final
  response, `view_file` = Read, `grep_search` = Grep, `list_dir` = Glob, `search_web` = WebSearch,
  `read_url_content` = WebFetch, `run_command` = Bash (or PowerShell, Claude Code on Windows), file writes =
  Write / Edit / MultiEdit / NotebookEdit, agy `conversationId` = Claude `agentId` (subagent) or `session_id`
  (main session), loading a skill = Skill tool.
- Hooks (`.claude/settings.json`): PreToolUse `pre_trade_guard.py` on Bash, PowerShell, MCP and file writes
  (Write, Edit, MultiEdit, NotebookEdit; deny = exit 2); PowerShell commands get the same decisions as Bash plus
  a stricter check on protected paths (only read-only cmdlets such as Get-Content may name them; encoded commands
  are denied); PostToolUse `post_trade_sync.py` and `post_pr_review_hook.py` (Bash and PowerShell); Stop
  `pr_review_stop_hook.py --claude`. Hooks are fail-closed exactly as in agy: if they are not active,
  live order execution is prohibited.

## Clean-room evaluation before any trade

1. `python3 scripts/prime_evaluator_brief.py` (add `--env testnet` only when the user asked for TESTNET).
2. Agent tool with `subagent_type: "isolated_market_evaluator"`, in the foreground, asking it to evaluate
   `logs/primed_brief.json` for the target environment. Never use a general-purpose agent or evaluate yourself.
3. Keep the `agentId` from the Agent result and run
   `python3 scripts/record_evaluation.py --from-claude-subagent <agentId>`. The recorder reads the
   `<dossier_json>` block from `~/.claude/projects/<slug>/<session>/subagents/agent-<agentId>.jsonl`, requires
   `agentType: "isolated_market_evaluator"` in its `.meta.json` and stamps sha256 provenance; the hook and the
   executor re-verify it and, in PROD, that the dossier came from this session.
4. Execute only via `python3 scripts/execute_futures_trade.py --symbol <S> --direction <D> ...` as approved
   (`--confirmed` only after the user's explicit "yes" for Tier A+/A). Full flow: the `trade-execution-planner` skill.

## PR review

`gh pr create` or a feature-branch push arms the review (PostToolUse); the Stop hook then asks you to run
the `pr-review` skill: triage, launch every required `<id>_reviewer` with the Agent tool in ONE message,
`python3 scripts/ci/assemble_review.py --pr <n> --from-claude-subagent <id>=<agentId> ...` (it verifies each
transcript's agentType), `verify_review.py`, then `gh pr comment <n> --body-file logs/pr_review/report.md`.

## Issue workflow

"Work issue N" runs the `issue-orchestrator` skill in the main session: `scripts/dev/issue_workspace.py init`
(worktree + branch), `issue_locator` (read-only), your `design.md`, `issue_fixer` (edits and tests; its edits, Bash and
Read/Grep/Glob go through `scripts/hooks/issue_fixer_guard.py`, Claude Code only and a confinement policy, not a sandbox: a
denylist plus path rules that keep it in the issue worktree, never the main checkout, with no git writes, gh,
network or desk scripts; `init` writes a binding marker the guard claims for the fixer's session and a per-issue
key in `logs/issue_work_keys/<N>.key` of the main checkout (gitignored, deleted by `cleanup`), with which the guard
signs its heartbeat (HMAC); `issue_workspace.py check-guard` verifies that signed heartbeat after round 1, and
`issue_workspace.py rebind <N> [--force]` resets a stale binding),
`issue_workspace.py review-context` + `issue_auditor` (read-only, up to 3 rounds), your own full-suite run, PR,
`pr-review`, merge on green CI, follow-up issues, `issue_workspace.py record-route` (appends to
`logs/issue_routing.jsonl`) and `issue_workspace.py cleanup`. Model and effort are routed per call by the skill's
route table (quick/build/deep via the Agent tool's `model`/`effort`); frontmatter `opus` is the fallback and agy
stays on opus. The three agents never use the internet; on Windows run the flow from WSL (the fixer's guard fails
closed without `python3`).

## Windows

Claude Code runs hooks with Git Bash, where `python3` is usually the Microsoft Store alias and the desk's
Python dependencies live in WSL. Copy `.claude/settings.local.json.example` to `.claude/settings.local.json`
(gitignored), replace `<WSL_DISTRO>` and `<REPO_PATH_IN_WSL>`, and run scripts as
`wsl.exe -d <WSL_DISTRO> -- python3 scripts/...`. Hooks from both files are merged: the `settings.json`
copies fail as non-blocking errors and the WSL copies make the decisions. Inside WSL the transcript lookup
also searches `/mnt/<drive>/Users/*/.claude/projects`. Native Git Bash does not set `WSL_DISTRO_NAME`: without
it, `wsl.exe -d <distro>` cannot verify the active distribution as the repository's own, so risk-reducing exits
ask for user confirmation rather than auto-allowing.
