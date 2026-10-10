---
name: issue-orchestrator
description: >-
  End-to-end GitHub issue workflow for this repository, run by the main agent: pick or take issue N,
  create its own worktree and fix/issue-N-* branch, map the code with the read-only issue_locator
  subagent, write the design decisions, implement with the issue_fixer subagent, audit each round
  with the read-only issue_auditor subagent (up to 3 rounds), verify the full suite, commit, push,
  open the PR, run the pr-review skill, merge when CI is green, open follow-up issues and clean up.
  Use when the user asks to work, fix or take an issue ("trabajá el issue 95", /issue-orchestrator
  95) or to pick the next one. Development work only: it never places orders.
---

# Issue Orchestrator

You (the main agent) orchestrate; three subagents with minimal tools do the focused work. Their model and effort are chosen per call by the issue's route (see Routing). Claude Code: pass the Agent tool's `model` and `effort`, which override the frontmatter; agy has no per-call model override, so all three stay on their frontmatter `opus`, which is also the fallback for any call without a `model`. `subagent_type` never changes, so the fixer guard and the read-only tool lists apply on every model. All three start with a clean context, so everything they need goes into files in the issue work directory and into a precise task message.

| Role | Subagent | Tools | Writes |
|---|---|---|---|
| Locator | `issue_locator` | read, grep, list (read-only) | nothing; its report is its final response |
| Fixer | `issue_fixer` | read, grep, list, edit, write, shell; edits and shell confined to the issue worktree by `scripts/hooks/issue_fixer_guard.py` (Claude Code only; a confinement policy, not a sandbox) | code, tests, `fixer_report.md` |
| Auditor | `issue_auditor` | read, grep, list (read-only) | nothing; its verdict is its final response |

Only the orchestrator uses the internet (Binance, GitHub or library docs), git history commands that write, `gh` and the desk scripts below. Subagents never get those tools.

**Work directory** (gitignored): `<WORKTREE>/logs/issue_work/` with `issue.json`, `locator.md`, `design.md`, `fixer_report.md`, `audit_round<k>.md`, `route_record.json` and `review/` (`diff.patch`, `files.txt`, `checks.json`, `checks.log`). Subagents exchange information only through these files, never through long pasted context.

## Rules

- This is development work, not a trading session: the trading code freeze does not apply, but never place orders, never run the executor, loops or ledger sync, and never edit runtime files under `logs/` other than `logs/issue_work/`. Two exceptions, both in the main checkout and written only by `issue_workspace.py`: `logs/issue_routing.jsonl` (`record-route`) and the issue's heartbeat key `logs/issue_work_keys/<N>.key` (`init` creates it, `cleanup` deletes it; gitignored, never read or copy it).
- One issue per worktree; never edit the main checkout (the routing log and the issue key above are the only exceptions). Every shell command in a worktree starts with `cd <WORKTREE> && ` (the working directory resets between calls).
- Write commit messages, PR bodies and issue bodies to files under `<WORKTREE>/logs/issue_work/` with the file-write tool and pass them with `-F` / `--body-file`: the pre-trade hook classifies whole command texts, and heredocs that mention protected logs are denied.
- Report to the user briefly in their language after each milestone (worktree ready, fixer done, audit verdict, PR, review, merge). Subagents' long outputs stay in files.

## Routing

`issue_workspace.py init` triages the issue deterministically from its labels and title and writes `routing` (`route`, `risk`, `reason`) to `issue.json`. First match wins:

| Route | Rule | Risk |
|---|---|---|
| `deep` | label `cat:risk_gate`, `severity:high` or `severity:critical`, or a title word naming the executor (`executor`, `execute_futures_trade`), a hook, guard or gate, the guardian, stop(-loss) verification or the evaluator prompt (`isolated_market_evaluator`); whole words only, so "aggregate" or "safeguard" do not match | high |
| `quick` | label `documentation`, or `severity:low` together with `cat:infra` or `cat:tool_error` | low |
| `build` | everything else, including no labels | medium |

Model · effort per role (Claude Code: Agent tool `model`/`effort`; agy: frontmatter opus for every role):

| Role | quick | build | deep |
|---|---|---|---|
| `issue_locator` | skipped (you map the 1-2 files yourself) | sonnet · medium | sonnet · high |
| `issue_fixer` round 1 | haiku · medium (only with a fully specified `design.md`), else sonnet · medium | sonnet · medium | opus · high |
| `issue_fixer` escalation (capability miss) | sonnet · medium | opus · medium | opus · xhigh (fable only with user approval) |
| `issue_auditor` | sonnet · medium (never haiku) | opus · high | opus · high |

- **Upgrade only.** You may upgrade the route after reading the issue or the locator report (e.g. a `quick` that touches a gate), never downgrade it; record the final route and the reason in `design.md` under "Route".
- **Haiku guard rails.** Haiku runs the round-1 fixer only when the route is `quick` AND `design.md` names every file, the exact change and the test to add or adjust; otherwise use sonnet. On `VERDICT: CHANGES_REQUESTED`, or a BLOCKED report or guard denial it could not resolve, escalate to sonnet: never retry haiku, never jump to opus.
- **Rollback.** Over the first 5-10 `quick` issues in the routing log, if haiku gets APPROVE in round 1 less than ~80% of the time, `quick` round 1 goes back to sonnet.
- `isolated_market_evaluator` and the pr-review `*_reviewer` models are out of scope and unchanged. Never switch the main session's model mid-issue.

## Steps

1. **Select the issue.** If the user named one, take it. Otherwise list open issues (`gh issue list --state open --json number,title,labels`) and pick by `severity:*` then `priority:*`. Skip issues whose likely files overlap open PRs (`gh pr list`), other worktrees (`git worktree list`) or other local agent sessions (ask them which files they touch and tell them yours). Prefer issues you can finish without user input. Read the last ~20 lines of `logs/issue_routing.jsonl` in the main checkout (if present) to see how routes and models performed.
2. **Create the worktree:** `python3 scripts/dev/issue_workspace.py init <N> --slug <short-kebab-slug>`. It fetches, creates `<repo>-wt-issue-<N>` on `fix/issue-<N>-<slug>` from `origin/main` and writes `issue.json` with its `routing` block. Read the issue and its route yourself.
3. **Research (orchestrator only).** If the fix depends on external behaviour (Binance endpoints, weights or error codes, GitHub or Claude Code features), look it up now and record the facts with their sources in `design.md`.
4. **Locate.** On `quick`, skip the locator and map the 1-2 files yourself. Otherwise launch the `issue_locator` subagent (Claude Code: Agent tool with `subagent_type: "issue_locator"` and the route's `model`/`effort`) with this task message: `WORKTREE=<abs path>. Map issue #<N> (logs/issue_work/issue.json) for the fixer: files, callers, persistence, tests and fixtures, docs, constraints and design options.` Save its report verbatim to `<WORKTREE>/logs/issue_work/locator.md`.
5. **Design.** Decide every open question yourself (consult an advisor when one is available and the issue is non-trivial), then write `<WORKTREE>/logs/issue_work/design.md` with: problem summary; "Route" (final route; reason if upgraded); numbered mandatory decisions (data sources, fail-closed behaviour in PROD vs TESTNET, function placement that keeps mocked tests valid, docs to update); "Do not touch" (files owned by other work, CLI flags another session's allowlist depends on); required tests; verification command; constraints (AGENTS.md byte cap, generated files, hermetic tests). The fixer must not invent design.
6. **Implement.** Launch the `issue_fixer` subagent (`subagent_type: "issue_fixer"`, round-1 `model`/`effort` from the route table) with: `WORKTREE=<abs path>. Issue #<N>, round 1. Implement logs/issue_work/design.md; follow your output contract.` Keep its id: later rounds continue the SAME fixer conversation (Claude Code: SendMessage to its agentId) so it keeps its context, unless step 7c escalates to a new fixer.
   - **Guard check.** Record the launch time (`date +%s`) right before launching the round-1 fixer. When round 1 ends, under Claude Code run `python3 scripts/dev/issue_workspace.py check-guard <WORKTREE> --since <launch_ts>` and read its output:
     - Exit 2 means the guard never wrote a fresh heartbeat signed with the issue key (`signed`, `sig_ok`), so the fixer may have run unguarded: stop and ask the user before any audit. Exit 2 is never a retry trigger: do not relaunch or re-prompt the fixer to produce a heartbeat. A legacy worktree without a key reports `signed: false` and is judged on the timestamp alone.
     - A fixer round with no Edit/Write/Bash call leaves no heartbeat (reads write none), so `check-guard` exits 2. Treat such a read-only round like any exit 2: stop and ask the user.
     - `session_id`: when it names the `<session>` directory that holds the fixer's transcript (`~/.claude/projects/<slug>/<session>/subagents/`), hook calls carry your session's id, so a new fixer in this session stays bound.
     - `marker_session_id_null`: `true` = the binding marker is not claimed yet (no guarded call carried a session id); `false` = the marker is bound to a session (some claim succeeded, even if the last `binding_claim` is `n/a`); `null` = no readable marker (a legacy worktree).
     - On `binding_claim: failed` (exit 0 with a `warning`) tell the user that the worktree stayed unbound.
     - Under agy no guard runs: before launching `issue_fixer`, tell the user that it would run unguarded with its prompt limits as the only confinement, ask whether to proceed, and launch it only after an explicit yes.
7. **Audit loop (max 3 fixer rounds).**
   a. Run `python3 scripts/dev/issue_workspace.py review-context <WORKTREE>` (diff plus untracked files, compileall, sync check and the full unittest run, written to `review/`; the checks run with the environment allowlist of `issue_workspace.py` `check_env` and a fresh empty `HOME`, while the fixer's own test runs are not scrubbed). If it reports `checks_ok: false`, send the failing checks (quoted from `review/checks.log`) straight back to the fixer BEFORE any audit (never audit a red suite): that is the next fixer round, under the round rules of (c), and it counts toward the 3-round budget; then repeat (a). Go to (b) only when `checks_ok` is true.
   b. Launch the `issue_auditor` subagent (`subagent_type: "issue_auditor"`, `model`/`effort` from the route table) with: `WORKTREE=<abs path>. Audit issue #<N>, round <k>.` On later rounds continue the same auditor so it re-checks its previous findings. Save its verdict to `<WORKTREE>/logs/issue_work/audit_round<k>.md`.
   c. `VERDICT: CHANGES_REQUESTED`: classify the miss and append a line `Miss: effort|capability — <why>` to `audit_round<k>.md`, then escalate one step:
      - **Effort miss** (skipped a file, did not run or update tests, ignored part of `design.md`): continue the SAME fixer with `Round <k+1>: resolve logs/issue_work/audit_round<k>.md (required changes only).` plus exactly what it skipped. Model and effort stay as they are (continuing a subagent cannot change them). Exception: a haiku round-1 fixer is never continued; even on an effort miss, start a new sonnet fixer as below.
      - **Capability miss** (full context, logic still wrong): start a NEW `issue_fixer` one step up (the escalation row) with `WORKTREE=<abs path>. Issue #<N>, round <k+1>. Implement logs/issue_work/design.md, resolving logs/issue_work/audit_round<k>.md; read your predecessor's logs/issue_work/fixer_report.md first.` Later rounds continue that new fixer.
      Then repeat from (a). You may also forward non-blocking notes that are small and safety-relevant (they then count as part of the round). Still at most 3 fixer rounds in total.
   d. `VERDICT: APPROVE`: continue. If round 3 ends without approval, stop: report the open findings to the user and ask how to proceed.
8. **Verify yourself.** Bring the branch up to date (`cd <WORKTREE> && git fetch -q origin`, then fast-forward or merge `origin/main`; stash only your own uncommitted changes around a fast-forward) and run the full gate in the worktree: `python3 -m compileall -q scripts/ tests/ && python3 scripts/dev/sync_claude_assets.py --check && python3 -m unittest discover tests/`. Never continue on a red suite.
9. **Commit, push, PR.** Stage exactly the intended files (never `logs/issue_work/`), commit with a message file ending in the attribution trailer your harness requires, `git push -u origin <branch>`, then `gh pr create --base main --head <branch> --title "<type>(<scope>): <summary> (#<N>)" --body-file <file>`. The body starts with `Closes #<N>` and covers the problem, the fix (decisions as implemented), tests and the exact suite result.
10. **PR review.** Run the `pr-review` skill (`.agents/skills/pr-review/SKILL.md`) on the PR, from the worktree, and post the report without asking. If a domain reviewer requires changes, run another fixer round with those findings (it counts toward the 3-round budget), re-verify, push and re-run the review.
11. **Merge.** When every reviewer approves and CI is green (`gh pr checks <n>`): if `origin/main` moved, integrate it, re-run the gate and push, wait for CI again; then `gh pr merge <n> --merge` and confirm the issue closed.
12. **Follow-ups (without asking).** Turn every non-blocking note from the auditor and the domain reviewers into new issues, grouped by theme, each with labels `severity:<low|medium|high>`, `priority:<P1|P2|P3>`, a category (`cat:risk_gate`, `cat:tool_error`, ...) and `bug` / `enhancement` / `documentation`; bodies with the problem, file:line evidence and acceptance criteria, written to files first.
13. **Record the route (before cleanup: `route_auto` is read from the worktree's `issue.json`).** Write `<WORKTREE>/logs/issue_work/route_record.json` with the file-write tool: `route_final`, `upgrade_reason` (required when it differs from the automatic route), `fixer_models` (one `{"model", "effort"}` per fixer round; effort `default` when none was passed, e.g. agy), `auditor_model`, `approved_round` (0 = never approved), `escalations` (`{"round": k, "kind": "effort"|"capability"}` for rounds 2..n) and `merged`. Then run `python3 scripts/dev/issue_workspace.py record-route <N> --from <WORKTREE>/logs/issue_work/route_record.json`; it appends one line to `logs/issue_routing.jsonl` in the main checkout (exit 2 on an invalid record or a downgrade: fix the file and rerun).
14. **Clean up:** `python3 scripts/dev/issue_workspace.py cleanup <N>` (removes the worktree, the local branch and the issue key only after the PR is merged; GitHub deletes the remote branch). If the pr-review Stop hook still reports a pending review for this already-reviewed PR, close it with `python3 scripts/ci/pr_review_state.py done --reason no_pr`.
15. **Report** to the user: PR link, what changed in plain terms, route and rounds needed, suite result, review link, follow-up issue numbers, and the suggested next issue. Pick the next issue only when the user asked for more than one.

## Failure handling

- A subagent that reports BLOCKED or a guard denial: read its report, fix the design or the environment yourself, then continue the same subagent (a haiku fixer that could not resolve it is replaced by a new sonnet fixer, see Routing). Never let a subagent work around a guard.
- A fixer denied with "bound to another session" after a step 7c escalation, or after your orchestrator session was restarted or resumed (a new session id): confirm no other session works in that worktree, then run `python3 scripts/dev/issue_workspace.py rebind <N>` (never edit the marker by hand), which sets `session_id` to null in `<WORKTREE>/logs/issue_work/fixer_binding.json`; the guard binds the worktree to the next session that works in it. `rebind` refuses (exit 2) while the guard heartbeat is younger than 10 minutes and carries a session id; once you have confirmed that no other session works there, rerun it with `--force`.
- A failing check you cannot attribute to the change (flaky or network-dependent test): rerun once; if it persists, record it as a follow-up issue and say so in the PR body.
- Never force-push, never merge with red CI or a CHANGES REQUIRED review, never delete a worktree whose PR is not merged unless the user says so (`cleanup <N> --force`).
