#!/usr/bin/env python3
"""
pre_trade_guard.py - PreToolUse Hook for the agentic runtime.
Deterministic pre-execution safety harness and programmatic risk verification.

Supported runtimes (output format is selected automatically):
  * Google Antigravity (agy), forced with `--agy`: stdin {toolCall:{name,args}, conversationId, ...};
    stdout {"decision": "allow"|"deny"|"ask"|"force_ask", "reason"}; ALWAYS exit 0
    (agy treats any non-zero exit code as a hook failure and drops the reason).
  * Claude Code (payload with tool_name/tool_input): deny -> exit 2 with the reason on stderr;
    otherwise exit 0 (JSON hookSpecificOutput.permissionDecision for "allow"/"ask").
  * Legacy (toolCall payload without --agy): {"decision", "code", "reason"}; deny exits 2.

Hardened against fail-open behaviors and spoofing vulnerabilities:
1. DEFAULT-DENY ON UNRECOGNIZED/EMPTY INPUT:
   Empty payload, invalid JSON, or malformed payload shape immediately return 'deny'.
2. SINGLE CHOKE POINT ENFORCEMENT:
   Opening trades are permitted ONLY via 'scripts/execute_futures_trade.py'.
   Binance MCP tools are checked against an ALLOWLIST of read-only tools (also when wrapped in the
   gateway meta-tool 'tool_execute'). Risk-reducing calls (reduceOnly=true, closePosition=true, cancel*)
   are allowed; every other Binance write tool is DENIED.
   The retired 'crypto_radar' MCP server (and its legacy tool names on any server alias) is DENIED
   outright, pointing to the CLI replacements (market-radar skill, executor flags, position guardian loop).
3. INLINE-CODE / RAW API BYPASS PREVENTION:
   run_command calls that use trading primitives outside the sanctioned scripts (python -c, heredocs,
   piped interpreters, curl/wget writes to Binance, unsanctioned scripts importing the engine) are denied.
   Batch deploy scripts and auto-deploy loops are treated as trade openings.
   Shell text is read through one bash-aware lexical pre-pass (_scan_shell, issue #98): '...', "...", $'...'
   (decoded), `...`, $(...) / $((...)) bodies (also inside "..."), backslash escapes, backslash-newline continuations
   (joined), comments (# at a word start outside quotes: removed) and heredoc operators outside quotes and comments
   (<<W, <<-W, <<'W', <<"W", <<\\W, several per line; not <<<). Heredoc bodies are cut out of the command text and
   tokenized on their own after their operator line, so a quote in a body or a comment never pairs with a later
   quote; an unterminated quote is confined to the lines from where it opens (the complete lines before it are
   judged as bash runs them). A scanner failure denies.
4. STRUCTURED RISK-REDUCING ACTION PARSING:
   MCP: reduceOnly=true / closePosition=true / cancel*. Shell: per sub-command, the script it actually executes
   (the program, the script operand of python, also through wsl.exe) must be one of RISK_REDUCING_SCRIPTS by its
   exact repo path, resolved lexically against the cwd (else the workspace root; inside wsl a relative or
   /mnt/<drive>/... path mapping to the root), never a name anywhere in the text, with an exclusive flag set:
   scripts/execute_futures_trade.py needs --close-position / --move-breakeven (exactly one --symbol) /
   --audit-orphans / --auto-heal / --protect-pending (or --help) and only {those, --positions, --symbol, --json,
   --force, --env, --help, -h, and --is-yolo next to --move-breakeven only}, but --move-breakeven --force (it
   overrides the anti-truncation and YOLO break-even-after-TP1 rules) is never auto-allowed: a forced break-even
   asks the user, never a denial (issue #111); scripts/loops/position_guardian_loop.py needs --once (never
   --interval) with {--env, --dry-run, --close-dead-alpha, --json}; scripts/loops/night_cutoff_loop.py {--env,
   --auto-ratchet, --overnight-mode}; scripts/trading_doctor.py needs --heal with {--env}; record_evaluation.py,
   loops/climax_watcher_loop.py and user_profile.py only --help / -h. An unknown flag, an abbreviation, a stray
   operand or another path is not risk-reducing (ask); an executor opening flag (--direction, --leverage, --margin,
   prices, --order-type, --is-yolo, --confirmed, --bypass-*, also abbreviated) sends the sub-command to the trade
   gates, except --is-yolo next to --move-breakeven (a break-even call off the sanctioned path asks, never denied).
   `execute_futures_trade.py --positions` alone is read-only (ask). The auto-allow also needs every other
   sub-command to be benign, no directory change in the line (cd / pushd / popd, and env -C / sudo -D / wsl --cd
   at any wrapper or wsl -e level), only RISK_WRAPPERS (env, timeout, nice, nohup, stdbuf) at every wrapper or
   wsl -e level (sudo in any form, doas, chroot, setsid, flock, time, ionice, taskset, exec, command, xargs, uv run
   ... change the user, root, shell or process context: issue #110), no wsl.exe -u / --user, no wsl.exe
   --shell-type login (it sources ~/.profile; standard / none are fine) and no wsl.exe -d /
   --distribution other than WSL_DISTRO_NAME (case-insensitive; unset: any -d asks), only RISK_ENV_ASSIGNMENTS
   (BINANCE_API_ENV, BINANCE_AUTH_MODE, PYTHONUNBUFFERED, PYTHONDONTWRITEBYTECODE, PYTHONIOENCODING,
   MSYS_NO_PATHCONV) and RISK_PYTHON_OPTIONS (-u, -B, -X utf8) before the script, no env -S, and no token of the
   risk-reducing sub-command holding ; | & $ < > ` or a line break (redirects included): otherwise "ask", never a
   denial. A gated trade opening on the same line as a risk-reducing sub-command that may not be auto-allowed asks.
   Paths compare lexically (_lexical_host_path, backslashes read as '/'): C:/x, c:/x and /mnt/c/x are one
   case-insensitive drive path; outside wsl and only when the session cwd is a Windows-side spelling (C:\\x, c:/x, a
   backslash, /c/x: a call made from Windows) Git Bash /c/x is that drive path too and //wsl.localhost/<distro>/x
   and //wsl$/<distro>/x are the Linux path /x (case-sensitive) when <distro> is WSL_DISTRO_NAME (case-insensitive;
   another or an unknown distro names another filesystem). With a native Linux cwd (/mnt/c/..., /home/...) or none,
   /c/x and //wsl.../x are Linux paths: not sanctioned (ask) (issue #110).
   Read-only analysis scripts (READ_ONLY_SCRIPTS, issue #191: scripts/trade_outcomes.py, scripts/trading_scorecard.py,
   scripts/exit_policy_sim.py; they place, change or cancel no order) are auto-allowed ("read-only analysis script")
   only as the single sub-command of a flat line, run by their exact repo path (never a linked worktree copy), with
   the same prefix / metacharacter / redirect rules as above, only their own flags, and every --output / --out /
   --outcomes value inside logs/ (lexically and by os.path.realpath); a write flag (--output / --out) must name the
   script's own output (never GROUND_TRUTH_FILES, READ_ONLY_FOREIGN_OUTPUTS or logs/evaluations/). The ground-truth
   check (8) lets such a script name its own ground-truth output (it is the sole sanctioned writer:
   trade_outcomes.py -> logs/trade_outcomes.jsonl, trading_scorecard.py -> logs/score_calibration.json); naming any
   other ground-truth file, also through --outcomes or a glob, stays denied (_names_only_own_ground_truth).
   Anything else asks, as before; next to other sub-commands they are not safe.
5. FAIL-CLOSED SESSION STATE & STALENESS CHECK (cache-based pre-check, defense in depth):
   logs/session_state.json must exist, be valid (is_valid=True) and NOT stale (<= 300s); its position count plus
   the same-env symbols of logs/pending_entries.json without an open position (issue #48; an unreadable or
   malformed registry denies in PROD) must stay below max_open_positions, and its delta_bias_incl_resting (else
   delta_bias, when missing or UNKNOWN; in PROD a missing or UNKNOWN value while the registry holds a same-env entry
   without an open position denies, issue #160) is checked too. The hook makes no network call, so this reads only the
   caches: the executor's live-anchored PROD gates (positionRisk, resting opening orders and the new order; issues
   #101 / #119) are authoritative and reject what a forged fresh file lets through here.
6. EVALUATION DOSSIER PROVENANCE (scripts/utils/dossier_provenance.py):
   PROD requires a schema v2 dossier whose provenance hash is re-verified against the
   isolated_market_evaluator subagent transcript (agy brain or Claude Code subagents/ with
   meta agentType), a matching direction and (when known) a parent conversation equal to the
   current one (agy conversationId / Claude Code session_id). TESTNET is relaxed.
7. EVALUATION TRAIL PROTECTION:
   Writes into logs/evaluations/, Antigravity brain transcripts or Claude Code subagent transcripts
   are denied, and so are agent-set transcript-root overrides (AGY_BRAIN_DIRS / CLAUDE_PROJECTS_DIRS);
   harness files (incl. .claude/agents/) and the gate modules (scripts/utils/gate_limits.py,
   scripts/execute_futures_trade.py, scripts/utils/portfolio_exposure.py, scripts/utils/env_resolver.py,
   scripts/user_profile.py; issue #79; scripts/utils/score_calibration.py, issue #202) require explicit confirmation (force_ask) from the file tools and from shell
   writes (redirect target, cp / mv / tee / rm, sed -i / perl -i, git checkout / restore, inline code that writes,
   PowerShell write cmdlets); running the executor or user_profile.py is never a write. So do file-tool writes to
   git config / hook files (GIT_EXEC_CONFIG_PATH_RE, see 8). File-tool content with trading primitives outside
   scripts/ and tests/ requires force_ask; scripts/ and tests/ of a linked git worktree of the same repository
   (its .git file and <common git dir>/worktrees/<name>/gitdir point at each other; issue #148) count as inside.
   Inside a linked worktree, HARNESS_FILES / HARNESS_DIRS do not apply, so <wt>/scripts/hooks/* gets a plain ask
   (worktree copies are not live hooks; they reach main only via PR review and CI; if a session is launched from
   a worktree, that worktree becomes base_dir and harness checks apply again).
8. GROUND TRUTH PROTECTION (GROUND_TRUTH_FILES):
   Runtime state that gates PROD orders has exactly one sanctioned writer, which writes it from Python:
   logs/session_state.json <- scripts/sync_session_state.py; logs/guardian_state.json (guardian liveness
   attestation for resting entries) <- scripts/loops/position_guardian_loop.py; logs/pending_entries.json
   (resting-entry registry / post-fill protection; also counted by this hook's max-open-positions pre-check, see 5)
   <- scripts/execute_futures_trade.py (registration and --protect-pending); logs/hook_heartbeat.json (hook
   liveness, see 11) <- this hook itself (issue #73); logs/score_calibration.json (Tier S score calibration,
   issue #202) <- scripts/trading_scorecard.py; logs/trade_outcomes.jsonl (the store's only input) <-
   scripts/trade_outcomes.py; logs/trades_audit.jsonl (entry ledger, the outcomes' source) <-
   scripts/execute_futures_trade.py; logs/primed_brief.json and logs/primed_brief_scores.json (evaluator brief and
   its radar scores) <- scripts/prime_evaluator_brief.py. File tools targeting them are denied: relative, absolute and Windows paths, NTFS aliases
   (trailing dot/space, ::$DATA streams) and targets whose os.path.realpath / samefile is a protected file
   (symlinked directory, hard link).
   Shell commands. The program of a sub-command is found past VAR=value / VAR+=value assignments, shell keywords
   (if / then / do / { / !) and wrappers with their options (env -u / -C / -S, sudo -u / -D, timeout -s SIG N,
   nice -n, stdbuf -oL, time -o, ionice, setsid, taskset, chroot, flock, doas, xargs -I {} -n 1 ...). A
   sub-command naming a protected file (literally or via a logs/ glob/brace word) is denied unless its program is
   read-only (GROUND_TRUTH_READ_PROGRAMS, jq without --in-place, python3 -m json.tool without an output file, git
   read sub-commands such as diff/log/commit, gh pr|issue, for-loop word lists, find judged below, python -c /
   node -e / python|node heredocs judged by write markers), has no write/exec option (git --output /
   --open-files-in-pager / --ext-diff / --upload-pack / --receive-pack / --exec / --extcmd and their unique-prefix
   abbreviations, short aliases per sub-command (clone/ls-remote -u, rebase -x, difftool -x; fetch/pull -u is
   --update-head-ok, its next word is judged as a command and still parsed as an operand), short clusters with O
   (-nOrm; value-taking shorts consume the rest, so -eOrder = -e Order), git -c / --config-env / --exec-path, rg
   --pre / --hostname-bin, less -o / -O / --log-file / +cmd) and no VAR= assignment. Value-taking options are
   skipped when collecting operands, from the rg 15.1 and git 2.43 help (rg -e/-d/-m/-t/--max-filesize/
   --hyperlink-format ..., git grep -A/-B/-C/-e/-f/-m and --threads/--max-depth/--max-count/--context including
   unique prefixes such as --thr 2). Any redirect (or time -o file) to a protected file is denied (also ')>',
   ';>', '<>').
   Line analysis (_ground_truth_line_hits): literal variables (D=logs, export D=logs, D+=x) are substituted in
   order and forgotten when reassigned at run time (D=$(...), read D, for D in, printf -v D); `cd` / `pushd` only
   ever ADD possible working directories (cd may fail, run in a subshell or after `false &&`), every sub-command is
   judged against each of them and the outer cwd; `cd` with no operand, `cd -`, `cd ~/x`, `cd "$X"`, `cd $(...)`
   and `popd` make it unknown, and then a write with a relative operand or redirect is denied (cd ~/x/logs && rm
   *.json); env -C dir / sudo -D dir change it for one sub-command; inside logs/ any relative write is denied.
   Run-time values ($VAR, ${VAR}, $(...), `...`, $1) left after substitution are denied in a write position: any
   operand of a write / destructive program (rm, mv, tee, touch, the destination of cp / ln / install / rsync,
   dd of=, the files of sed -i / perl -i, Windows delete / move / copy), a redirect target, an output option
   (--output=, --log-file=, curl -o, sort -o, wget -O, tar -C / -f / -g, unzip -d), the root of a destructive find,
   an archive destination, git -C / --work-tree of a writing git sub-command, and a program known only at run time
   ($RM, $(which rm)) next to logs/, a protected file or another run-time value. $(pwd), `pwd`, $PWD and $(git
   rev-parse --show-toplevel) count as '.', $(cmd)/path is a run-time path ($X/logs counts as logs/), every other
   $(cmd) / `cmd` is lifted (from the raw text, comments and heredoc bodies included) and judged as its own line,
   and a quoted line break is an argument, not a separator. The line is read through the lexical pre-pass (see 3):
   comments never hide a line, a heredoc body is judged as shell lines isolated from the text around it (scanned
   as a shell would read it and, when that scan removed a comment or let a quote span lines, also line by line,
   since the body may be data or another language where # is not a comment), and with an unterminated quote the
   complete lines before the one where it opens are judged together, the rest line by line.
   Nested command lines get the same full, strict line analysis one level deeper, started once from every
   possible cwd of the enclosing line (beyond NESTED_DEPTH_LIMIT levels the command is denied): sh/bash -c, eval, cmd /c, powershell / pwsh -Command and -EncodedCommand in any prefix
   spelling (-e, -ec, -en, -enc, -enco ... -encodedcommand, with -, -- or /, or -enc:payload; base64 UTF-16LE,
   missing or undecodable -> deny), env -S, flock / su / runuser / script -c, watch, git -c values, git bisect run /
   submodule foreach, the values of command-running options (git -O<cmd> / --upload-pack / --receive-pack /
   --exec / --extcmd, rg --pre / --hostname-bin, less +!cmd, tar --to-command / -I / -F / --rsh-command /
   --checkpoint-action=exec=), the shell calls of inline code (os.system / subprocess.* / popen / exec* /
   child_process.exec* / perl system / qx / ruby %x / awk system(): their string literals), find -exec commands
   ({} = a root the filters can match or logs/ under it), whether or not a protected file is named. A
   command-running option that runs on files (rg --pre, git grep -O) or on a local repository (git fetch
   --upload-pack, push --receive-pack / --exec) is denied when an operand (or the default '.', or the git -C base
   dir) is logs/ or one of its ancestors (rg --pre rm . logs, git -C logs grep -O x -- '*.json').
   Shell script files: a shell's script operand (options parsed: bash -o errexit f, sh -e -x f, bash --norc f,
   --rcfile f), source f / . f, and a program invoked by path (./evil, /tmp/evil, scripts/x, f.sh) that is a text
   file with a sh / bash / zsh / dash / ksh shebang (also /usr/bin/env [-S] bash) or none, have their content judged
   by the same strict line analysis (whole-line comments dropped; a program invoked by path is classified from its
   first SCRIPT_HEAD_BYTES (4 KiB) and read further only when it is a shell script) - denied when larger than
   SCRIPT_READ_LIMIT (256 KiB),
   unreadable or not UTF-8, when the same command line (or an enclosing one) writes the script (redirect, tee, cp,
   heredoc, inline code naming it, git checkout; an archive extraction or recursive copy may write any name), or
   when its path is a run-time value / relative to an unknown cwd; a missing file judges as nothing. A shell reading
   commands from stdin (... | bash, bash -s, sh -s, source /dev/stdin, bash <(curl ...)) is denied unless its input
   is visible: a heredoc (its body is judged line by line, isolated from the rest, also when the operator sits in
   a "$(...)" string unless a plain `cat` reads it for a git / gh argument: a commit message), a here-string (judged), `< file` or a single `cat file` /
   `cat < file` producer (the file is judged). Only DESK_SHELL_SCRIPTS (scripts/report_issue.sh, resolved against
   the workspace root and pinned by the sha256 of the bytes the hook read) is judged without the run-time-value and
   unknown-cwd rules; every other rule (literal operands, xargs, stdin shells, nested lines) still applies, scripts
   it runs or sources and every other file under scripts/ (or an edited copy) are strict, and the file is a harness
   file (edits require confirmation).
   Work budget: one hook evaluation shares a fail-closed budget across nested lines, scripts and cwd candidates
   (AUDIT_MAX_SUBCOMMANDS = 5000 sub-command analyses, AUDIT_DEADLINE_SECONDS = 3.0 s monotonic); past it the
   command is denied as too complex to audit. Nested lines and scripts are memoised per (text, cwds, flags) within
   the evaluation.
   xargs (also behind nice / timeout / env / command / exec / stdbuf ...; options parsed per GNU findutils: -I {},
   -i, -n, -P, -L, -s, -E, -d, -0, -a) running anything but a read-only program is denied unless every producer of
   its pipe is a find over literal roots that are neither logs/, below it nor an ancestor of it (also by realpath;
   relative roots need a known cwd), without -L / -follow and printing only the paths, and the command is rm /
   rmdir / unlink or cp / mv / ln / install -t DIR: no producer (xargs rm < list), -a FILE, -d DELIM, -I / -i, a
   shell, an interpreter, env or a wrapper -> denied.
   Env vars: injection and command channels are denied outright on any command (RIPGREP_CONFIG_PATH,
   GIT_CONFIG_PARAMETERS / COUNT / GLOBAL / SYSTEM / KEY_* / VALUE_*, GIT_EXEC_PATH, GIT_TEMPLATE_DIR, GIT_DIR,
   GIT_WORK_TREE, GIT_COMMON_DIR, GIT_INDEX_FILE, PROMPT_COMMAND, LD_PRELOAD, LD_AUDIT, LD_LIBRARY_PATH,
   GIT_EXTERNAL_DIFF, GIT_SSH, GIT_SSH_COMMAND, GIT_ASKPASS, SSH_ASKPASS, GIT_PROXY_COMMAND, LESSOPEN, LESSCLOSE,
   GIT_ALLOW_PROTOCOL, GIT_PROTOCOL_FROM_USER);
   ENV / BASH_ENV when the program is a shell, env, a script run by path or nothing (standalone, export); pager vars
   (GIT_PAGER, PAGER, MANPAGER) accept only an empty value or cat / less / more with read-only flags (no +cmd,
   -o / -O / -k, --log-file), LESS only such flags, editor vars (EDITOR, VISUAL, GIT_EDITOR, GIT_SEQUENCE_EDITOR)
   only true / : / cat; non-literal values are denied. This applies to VAR=v cmd, VAR+=v, assignments after
   wrappers (nice env X=v, time X=v, sudo X=v, env -S 'X=v cmd'), export / declare / local / readonly, and read /
   printf -v / mapfile / for / declare -n of these names.
   Git config: GIT_CONFIG_DANGEROUS_RE keys (core.pager / editor / sshCommand / fsmonitor / hooksPath / worktree,
   pager.*, alias.*, diff.external, diff.*.textconv, filter.*, remote.*.uploadpack, credential[.*].helper,
   include[If].path, submodule.*.update, hook.*.command ...; case-insensitive) are denied in `git -c key=value`,
   `git clone -c`, `--config-env` (hidden value: any dangerous key) and persistent `git config` (options with
   values parsed: -f / --file, --blob, -t / --type, --default, --comment, --value; scopes, -z, --name-only; the
   get / set / unset / list / rename-section / remove-section / edit syntax), except pager keys with an allowlisted
   pager or a boolean, editor keys with true / : / cat, and protocol.allow / protocol.<name>.allow with never.
   `git config` is a read only with an explicit read action (--get*, -l / --list, get, list); edit /
   rename-section are denied; clone / init --template are denied; a writing git sub-command with -C / --work-tree
   inside logs/ is denied; any git argument starting with ext:: (a transport that runs a shell command) is denied.
   The same channel through the files themselves (GIT_EXEC_CONFIG_PATH_RE, matched on the normalised path
   whatever the root, case-insensitive: .git/config, .git/config.worktree, .git/hooks[/...],
   .git/worktrees/<x>/config* | commondir | gitdir, .git/modules/.../config | hooks, a bare .git gitdir file,
   any .gitconfig* basename, .config/git/config, /etc/gitconfig; also through a glob whose dot-component can
   expand to .git / .gitconfig; not .git/info/, whose exclude / attributes / sparse-checkout run nothing by
   themselves) is denied with its own reason (Git Config Channel Protection): a redirect, the write targets of a
   writer (tee, cp / install / rsync / mv destination, chmod / touch, dd of=, sed / perl -i, Windows copies /
   moves), every operand of a link (ln, link, cp -l / -s / --link / --symbolic-link, rsync --link-dest: a link
   aliases its source, ln -s .git/config x; echo y >> x), an output option (curl -o, --output=), an archive
   destination, relative words against the tracked cwd (cd .git/hooks && echo x > pre-commit), inline code with a
   write call next to such a string literal, and (catch-all, Bash and PowerShell) any other program outside the
   read-only allowlist and GIT_EXEC_CONFIG_MODELLED_PROGRAMS naming one in an argument (patch, sponge, ed, ex,
   vim -c wq, awk -i inplace, perl -e 'open(F,q(>>.git/config))', code .git/config, Set-Content, Out-File ...;
   du / tree / od / hexdump / nl and xxd without -r read). HOME= /
   XDG_CONFIG_HOME= in front of git (prefix or env) and git --git-dir[=]<x> are denied like GIT_DIR. git itself
   is otherwise left to the key rules above (git config -f .git/config user.name x, git commit -F
   .git/COMMIT_EDITMSG stay allowed), and reads (cat, grep, Get-Content, git config --get / -f F --list, cp
   .git/config /tmp/x), pure deletions (rm, del) and moving a hook away (mv .git/hooks/x /tmp) are unaffected.
   Recursive / glob copies and archive extraction into the repo root (cp -r /tmp/e/.git ., tar -xf x.tar) are
   already denied above as writes into an ancestor of logs/.
   Also denied: inline interpreters (Python / Node / Perl / Ruby) with write calls naming them, or with destructive
   calls next to a 'logs' literal / logs/ glob (rmtree, remove_tree, rmSync, unlink, rename, FileUtils.rm_rf,
   File.delete, Dir.rmdir ...) or recursive deletes/moves next to an ancestor literal ('.', '..', the repo). Both
   are checked on the whole command line, so a `git commit -m "$(cat <<EOF ...)"` or `gh ... --body "$(...)"` text
   naming a protected file next to a write marker such as `.write(` is denied: use -F <file> / --body-file. Heredoc
   bodies are only exempt from the line-by-line check when terminated and fed to python/node (python3 - <<EOF, cat <<EOF | node; a
   << inside $((...)) / ((...)) is a shift, never such a heredoc) or read by a plain `cat` inside a "$(...)" argument of git / gh: a
   message argument. Also: destructive find whose filters
   can match them or that is unfiltered/negated over a root that is or contains logs/ (., .., /, ~, the repo),
   recursive rm / rd / del / Remove-Item / mv / move of logs/ or an ancestor (globs and braces expanded: log*,
   {logs,build}), recursive / glob / --files-from copies into logs/ or an ancestor (incl. -t/--target-directory,
   cp -r src/. ., rsync -a src/ ./), Windows copies into logs/ (robocopy, xcopy, any recursive or wildcard copy, a
   source that is not an existing regular file; `copy a.txt logs` stays allowed) or recursive ones (robocopy /S /E
   /MIR /PURGE, xcopy /S /E, Copy-Item -Recurse) into an ancestor, archive extraction (tar -x / --extract / --get /
   old-style xf, unzip, 7z x, Expand-Archive; tar -t and -xO are reads, tar -czf only creates) whose destination
   (-C, -d, -o, -DestinationPath, else the cwd) is logs/ or an ancestor, tar output files (--index-file, -g)
   naming a protected file, symlinks/hard links (ln, mklink) aliasing logs/, git clean -x/-X and git stash --all.
   `_is_logs_dir` counts ANY path whose last component is logs (relative, POSIX absolute, Windows / UNC /
   drive-mount form, $X/logs, ~/logs, and so also /tmp/other/logs, build/logs): symlinks, /proc/<pid>/cwd and
   junctions alias the workspace logs/ in ways the hook cannot resolve; globs (log*, *) are resolved against the
   workspace root and tracked cwd. A cd / pushd / env -C / sudo -D into a directory named logs counts as inside
   logs/, and a path through /proc/<pid>/{cwd,root,fd} or /dev/fd (a write operand, cd target or find root) is
   unresolvable: an unknown cwd / ancestor of logs/. PowerShell statements are judged
   per sub-command with these rules minus the Bash-only ones (cd / variable / xargs / stdin-shell tracking, run-time
   values, scripts by path; see 9). Reads by allowlisted programs keep the normal permission policy (ask).
   wsl.exe without -e / --exec (Bash and PowerShell alike): its default Linux shell parses the arguments again, so
   their plain space-joined text (after `cd DIR &&` for --cd DIR) is judged once more as a Bash line by the whole
   decision engine (nested wsl calls up to NESTED_DEPTH_LIMIT levels, deeper is denied); the most restrictive
   result wins. The join drops the quoting Git Bash / PowerShell may add around arguments with spaces, so it can
   split differently from Linux (wsl.exe -- bash -c 'rm -rf logs' re-parses as bash -c rm ...): it is an extra
   check, not a complete model, which is why the command's own tokens are judged too. Every wsl.exe [options]
   [-e | --] <cmd>
   (Bash and PowerShell alike) also has <cmd>'s own tokens (quoting kept) judged as a Linux sub-command (from an
   unknown cwd after --cd), so wsl.exe -e rm -rf logs and wsl.exe -- bash -c 'rm -rf logs' are denied; a Bash
   line still goes through every rule with its outer tokens (PAGER=most wsl.exe -e git log, wsl.exe -e ls > $Y).
   Not covered (residual, defense in depth; the executor-side fail-closed checks are the primary control): Python /
   Node / other non-shell script files (run by their interpreter or by path with their shebang), interpreters
   reading code from a file we do not open (PYTHONPATH / sitecustomize, NODE_OPTIONS --require, PERL5OPT), command
   text only known at run time (eval "$(...)", a $(cmd) program with no operands), writes by programs whose
   semantics we do not model (make, docker, parallel, ssh to localhost ...), git commands run in another
   repository whose config the agent wrote (git -C /tmp/r status), git config files only named at run time
   ($XDG_CONFIG_HOME/git/config is denied as a run-time write, a config file pulled in by include.path from an
   arbitrary path is not recognised), file targets of a sed script's own w / W / s///w commands naming a git
   config path (sed 'w .git/config' x), run-time values and unknown cwd inside the pinned scripts/report_issue.sh,
   a missing pending_entries.json still reads as empty in the executor (tracked as follow-ups), and the lexical
   pre-pass's own simplifications: a `case` command inside $(...) is denied outright, also a multi-line one (n=$(...
   while read f; do case "$f" in *.py) ...;; esac; done)), since its pattern `)` would close the substitution early
   (outside $(...) it is harmless; in a heredoc body that is data, not run by a shell, the body falls back to the
   line-by-line check instead), a multi-line $((...)) / ((...)) holding << is denied too (a $( (subshell) )
   heredoc or a shift), ${...} is
   not a quoting context, a heredoc operator inside `...` is not recognised, a multi-line $(...) inside "..." stays
   one argument (only its heredoc bodies are judged), and a heredoc body ends at the first line equal to its
   delimiter (more lines are judged as commands than bash would run, the strict direction).
9. CLAUDE CODE POWERSHELL TOOL (Windows) AND NOTEBOOKEDIT:
   NotebookEdit is a file tool (notebook_path). PowerShell commands are scanned for analysis only (Unicode quotes
   and dashes mapped to ASCII; quote-aware: '...' literal, backtick escapes in "..." and bare text; comments
   dropped; backslashes -> '/'); unbalanced quotes / brackets / block comments are denied. Each body of {...},
   $(...) (also inside "..."), @(...) and (...) is judged recursively as its own command (depth limit), the
   statement itself with the bodies replaced by a placeholder, and the whole text once more for denials; the most
   restrictive result wins and a command with bodies is never auto-allowed. Statements are judged exactly like Bash
   (trading primitives, deploy batches, raw Binance HTTP, evaluation trail, harness, ground truth; read-only cmdlets
   such as Get-Content / Select-String may name ground-truth files; wsl.exe [options] -- <cmd> is judged as the
   Linux command and its joined arguments once more as a Bash line, as in 8).
   Git config / hook paths next to a write construct are denied (Git Config Channel Protection, see 8). This
   backstop is coarse on purpose: a read that also writes elsewhere (Get-Content .git/config > out.txt,
   Copy-Item .git/config backup.txt) is denied too. A backstop then denies a command naming a ground-truth file
   (literally, through a
   logs/ glob or an 8.3 short name), the evaluation trail or the logs/ directory unless every statement starts with
   a read-only / navigation cmdlet and nothing in it can write or run code (write cmdlets and aliases anywhere,
   redirection other than to $null, .NET static / method calls, ${provider:path} variables, iex / Invoke-Command /
   Start-Process / ForEach-Object, call operator, dot-sourcing, %); a glob that can expand to logs/ (*, log*)
   counts only next to such a construct. Encoded payloads (powershell/pwsh -EncodedCommand / -enc / -ec / -e,
   FromBase64String executed), Invoke-RestMethod / Invoke-WebRequest writes to Binance and unparseable commands
   (unbalanced quotes) are always denied. Not covered: names assembled at runtime (string concatenation, variables).
   Harness paths next to such a construct require confirmation (force_ask); for scripts/execute_futures_trade.py
   and scripts/user_profile.py (run as programs) a mention counts unless it is the script a Python interpreter
   runs as the statement's program (python / py / & C:\\...\\python.exe / wsl.exe -- python3 <path>, only
   interpreter options in between), also inside (...) or a $variable assignment (issue #79).
10. PASS-THROUGH:
   Tool calls unrelated to trading return "ask" so the runtime's normal permission policy applies.
   "allow" is reserved for calls that passed every trading gate or are purely risk-reducing (4), written as one
   flat single-line command; anything the analysis cannot vouch for is downgraded to "ask".
11. HEARTBEAT:
   Every invocation refreshes logs/hook_heartbeat.json (best effort, never alters the decision). The file is
   ground truth (8) written only by this hook from Python: an agent command that merely names it outside a
   read-only program is denied like the other ground-truth files (report_issue.sh: pass such text through
   --context-file / --output-file).

Target latency: < 15ms (plus dossier provenance re-verification on trade openings, and reading / judging the shell
script files a command runs).
"""

import os
import sys
import json
import time
import re
import shlex
import fnmatch
import hashlib
import contextlib
import functools
import ntpath
import posixpath
import datetime
from typing import Dict, Any, Tuple, Optional, List

# Ensure scripts directory is on sys.path for utils
base_script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if base_script_dir not in sys.path:
    sys.path.insert(0, base_script_dir)

try:
    from utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
except ImportError:
    try:
        from scripts.utils.env_resolver import resolve_env, is_prod_environment, find_workspace_root
    except ImportError:
        def find_workspace_root() -> str:
            p = os.path.abspath(__file__)
            while p and p != os.path.dirname(p):
                p = os.path.dirname(p)
                if os.path.exists(os.path.join(p, "AGENTS.md")) or os.path.exists(os.path.join(p, "logs")):
                    return p
            return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

        def resolve_env(explicit_env=None, base_dir=None) -> str:
            val = (explicit_env or os.environ.get("BINANCE_API_ENV") or "prod").strip().lower()
            return "prod" if val in ["prod", "production", "mainnet"] else "testnet"

        def is_prod_environment(explicit_env=None, base_dir=None) -> bool:
            return resolve_env(explicit_env, base_dir=base_dir) == "prod"

try:
    from utils import dossier_provenance as dp
except ImportError:  # pragma: no cover - fail closed below when the module is missing
    dp = None

try:
    from utils import score_calibration as scal  # stdlib-only, reads the local store (issue #202)
except ImportError:  # pragma: no cover - fail closed: an unconfirmed Tier S asks the user
    scal = None

try:
    from utils.atomic_writer import atomic_write_json
except ImportError:  # pragma: no cover
    atomic_write_json = None


HOOK_NAME = "pre_trade_guard"
HEARTBEAT_ENV_OVERRIDE = "PRE_TRADE_GUARD_HEARTBEAT_FILE"

CHOKE_POINT = "'scripts/execute_futures_trade.py'"
EVALUATOR_HINT = (
    "Invoke the clean-room evaluator via invoke_subagent with TypeName 'isolated_market_evaluator', "
    "then record its verdict with `python3 scripts/record_evaluation.py --from-subagent <conversationId>` "
    "(Claude Code: Agent tool with subagent_type 'isolated_market_evaluator', then "
    "`python3 scripts/record_evaluation.py --from-claude-subagent <agentId>`)."
)

# -----------------------------------------------------------------------------
# Tool name classification
# -----------------------------------------------------------------------------
AGY_MCP_CALL_TOOLS = {"call_mcp_tool", "mcp_tool"}
FILE_WRITE_TOOLS = {"write_to_file", "replace_file_content", "multi_replace_file_content"}
CLAUDE_FILE_WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
# Claude Code shell tools -> shell dialect (PowerShell: Claude Code on Windows)
CLAUDE_SHELL_TOOLS = {"Bash": "bash", "PowerShell": "powershell"}
# Retired servers stay listed so eager names (mcp_crypto_radar_<tool>) still split correctly.
KNOWN_MCP_SERVERS = ("crypto_radar", "binance")

# The crypto_radar MCP server is retired: every call is denied (fail closed if a stale client config still
# starts it). Its legacy tool names are denied on any server alias and mapped to their CLI replacements.
RETIRED_MCP_SERVERS = {"crypto_radar"}
LEGACY_RADAR_TOOL_REPLACEMENTS = {
    "scan_intraday_market": "python3 scripts/broad_market_radar.py --json",
    "scan_yolo_moonshot": "python3 scripts/broad_yolo_scanner.py --json",
    "scan_delta_neutral_pairs": "python3 scripts/quant_risk_engine.py pairs --json",
    "calculate_volatility_parity": "python3 scripts/quant_risk_engine.py parity --json",
    "calculate_position_sizing": "python3 scripts/quant_risk_engine.py parity --json",
    "get_empirical_kelly_audit": "python3 scripts/quant_risk_engine.py kelly --json",
    "get_crypto_newsletters": "python3 scripts/fetch_newsletters.py --format json",
    "deploy_futures_trade": "python3 scripts/execute_futures_trade.py --symbol <SYMBOL> --direction <LONG|SHORT> ... "
                            "(after the clean-room evaluation)",
    "place_order": "python3 scripts/execute_futures_trade.py --symbol <SYMBOL> --direction <LONG|SHORT> ... "
                   "(after the clean-room evaluation)",
    "get_open_positions": "python3 scripts/execute_futures_trade.py --positions --json",
    "move_to_breakeven": "python3 scripts/execute_futures_trade.py --move-breakeven --symbol <SYMBOL>",
    "move_sl_to_breakeven": "python3 scripts/execute_futures_trade.py --move-breakeven --symbol <SYMBOL>",
    "close_position_market": "python3 scripts/execute_futures_trade.py --close-position --symbol <SYMBOL>",
    "close_position": "python3 scripts/execute_futures_trade.py --close-position --symbol <SYMBOL>",
    "audit_orphan_positions": "python3 scripts/execute_futures_trade.py --audit-orphans (or --auto-heal)",
    "update_trailing_stop_structural": "python3 scripts/loops/position_guardian_loop.py --once",
    "audit_and_trail_all_positions": "python3 scripts/loops/position_guardian_loop.py --once",
    "check_dead_alpha": "python3 scripts/loops/position_guardian_loop.py --once --dry-run",
    "report_agent_execution_issue": "./scripts/report_issue.sh --title ... --error ...",
}

# Binance MCP product namespaces (dotted names such as futures_usds.newOrder)
BINANCE_NAMESPACE_RE = re.compile(
    r"^(?:futures_usds|futures_coin|spot|margin|wallet|convert|sub_account|analysis|algo|alpha|"
    r"copy_trading|simple_earn|staking|portfolio_margin\w*|derivatives_\w+|c2c|fiat|mining|pay|rebate|"
    r"vip_loan|crypto_loan|dual_investment|gift_card)\.[A-Za-z0-9_]+$"
)
# Gateway naming pattern `{verb}_{product}_{operation}` (e.g. create_spot_newOrder)
BINANCE_VERB_RE = re.compile(
    r"^(get|create|delete|update|put|post|cancel)_((?:futures_usds|futures_coin|spot|margin|wallet|convert|"
    r"sub_account|[a-z]+)(?:_[a-z]+)*)_([A-Za-z0-9]+)$"
)
BINANCE_META_READ_TOOLS = {"tool_search"}
BINANCE_META_EXECUTE_TOOLS = {"tool_execute"}

# Read-only Binance operations (lower-case operation names, namespace-agnostic). Derived from
# .agents/skills/binance/references/*.md and the tool names observed in agy transcripts.
BINANCE_READ_ONLY_OPS = frozenset(op.lower() for op in [
    # futures_usds - account
    "accountInformation", "accountInformationV2", "accountInformationV3",
    "futuresAccountBalance", "futuresAccountBalanceV2", "futuresAccountBalanceV3",
    "futuresAccountConfiguration", "futuresTradingQuantitativeRulesIndicators", "getBnbBurnStatus",
    "getCurrentMultiAssetsMode", "getCurrentPositionMode", "getDownloadIdForFuturesOrderHistory",
    "getDownloadIdForFuturesTradeHistory", "getDownloadIdForFuturesTransactionHistory",
    "getFuturesOrderHistoryDownloadLinkById", "getFuturesTradeDownloadLinkById",
    "getFuturesTransactionHistoryDownloadLinkById", "getIncomeHistory", "notionalAndLeverageBrackets",
    "queryUserRateLimit", "symbolConfiguration", "userCommissionRate",
    "classicPortfolioMarginAccountInformation",
    # futures - market data
    "adlRisk", "assetIndex", "multiAssetsModeAssetIndex", "basis", "checkServerTime",
    "compositeIndexSymbolInformation", "compressedAggregateTradesList", "continuousContractKlineCandlestickData",
    "exchangeInformation", "getFundingRateHistory", "getFundingRateInfo", "indexPriceKlineCandlestickData",
    "klineCandlestickData", "longShortRatio", "markPrice", "markPriceKlineCandlestickData", "oldTradesLookup",
    "openInterest", "openInterestStatistics", "orderBook", "premiumIndexKlineData",
    "quarterlyContractSettlementPrice", "queryIndexPriceConstituents", "queryInsuranceFundBalanceSnapshot",
    "recentTradesList", "rpiOrderBook", "symbolOrderBookTicker", "symbolPriceTicker", "symbolPriceTickerV2",
    "takerBuySellVolume", "testConnectivity", "ticker24hrPriceChangeStatistics",
    "topTraderLongShortRatioAccounts", "topTraderLongShortRatioPositions", "tradingSchedule",
    # futures - trade (queries only)
    "accountTradeList", "allOrders", "currentAllAlgoOpenOrders", "currentAllOpenOrders",
    "futuresTradfiPerpsContract", "getOrderModifyHistory", "getPositionMarginChangeHistory",
    "positionAdlQuantileEstimation", "positionInformation", "positionInformationV2", "positionInformationV3",
    "queryAlgoOrder", "queryAllAlgoOrders", "queryCurrentOpenOrder", "queryOrder", "testOrder",
    "usersForceOrders",
    # spot
    "depth", "exchangeInfo", "getAccount", "getOpenOrders", "getOrder", "getAllOrders", "klines", "myTrades",
    "ticker24hr", "tickerPrice", "uiKlines", "avgPrice", "bookTicker", "ticker", "tickerTradingDay",
    "trades", "historicalTrades", "aggTrades", "ping", "time",
    # margin
    "crossMarginCollateralRatio", "getAllIsolatedMarginSymbol", "getAllMarginAssets",
    "queryCrossMarginAccountDetails", "queryMarginAccountsAllOrders", "queryMarginAccountsOpenOrders",
    "queryMarginAccountsOrder", "queryMarginAccountsTradeList", "queryMaxBorrow",
    # wallet / sub-account / convert / analysis
    "accountStatus", "allCoinsInformation", "dailyAccountSnapshot", "depositAddress", "depositHistory",
    "queryUserUniversalTransferHistory", "queryUserWalletBalance", "withdrawHistory", "getApiKeyPermission",
    "systemStatus", "getMainAccountAsset", "getConvertTradeHistory", "listAllConvertPairs", "orderStatus",
    "queryLimitOpenOrders", "queryOrderQuantityPrecisionPerAsset", "getTokenAiReport",
])
BINANCE_REDUCE_ONLY_ORDER_OPS = {"neworder", "newalgoorder"}
BINANCE_BATCH_ORDER_OPS = {"placemultipleorders"}

# -----------------------------------------------------------------------------
# run_command classification
# -----------------------------------------------------------------------------
SHELL_SEPARATORS = {"&&", "||", ";", "|", "&", "\n", ";;", "|&", "(", ")"}
REDIRECT_TOKENS = {">", ">>", ">|", "&>", "&>>", ">&", "<>"}
SHELL_PUNCTUATION = "();<>|&\n"
# Stands for a line break inside a '...' / "..." string (an argument, not a command separator)
QUOTED_NEWLINE_SENTINEL = "__newline__"
WORD_BOUNDARY_CHARS = " \t\r\n;&|()<>"
# bash $'...' escapes with a fixed value (octal, \x, \u, \U and \c are decoded by _ansi_c_decode)
ANSI_C_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t",
                  "v": "\v", "\\": "\\", "'": "'", '"': '"', "?": "?"}
# Heredocs inside heredoc bodies are tokenized recursively; deeper nesting, or more operators in one text (each
# one's head is parsed again), is denied as too complex to audit (fail closed)
HEREDOC_NESTING_LIMIT = 16
HEREDOC_MAX_PER_SCAN = 64
# Runs of characters with no lexical meaning for _scan_shell in a command context / inside "..."
SCAN_PLAIN_CMD_RE = re.compile(r"[^\\'\"$`()#<\n]+")
SCAN_PLAIN_DQ_RE = re.compile(r"[^\\\"$`\n]+")
HEREDOC_OPERATOR_RE = re.compile(r"(?<!<)<<(?!<)")
# Programs whose "$(cat <<'EOF' ... EOF)" argument is message text (commit / PR bodies), not judged as shell lines
HEREDOC_MESSAGE_PROGRAMS = {"git", "gh"}
# Besides SHELL_INTERPRETERS, programs that run a heredoc body as shell code (a scan failure there denies)
HEREDOC_SHELL_READERS = {"eval", "source", ".", "wsl", "xargs", "su", "script", "busybox"}
# bash operators, longest first: shlex returns punctuation runs (')>', ';>', '<>') as one token
SHELL_OPERATORS = ("&>>", "<<<", "&&", "||", ";;", "|&", ">>", ">|", ">&", "&>", "<>", "<<", "<&",
                   "(", ")", ";", "|", "&", "\n", ">", "<")
INSPECTION_PROGRAMS = {
    "git", "gh", "grep", "rg", "cat", "ls", "find", "diff", "pytest", "cp", "rm", "mkdir", "chmod",
    "echo", "printf", "head", "tail", "less", "wc", "stat", "file", "jq", "sort", "uniq", "awk",
    "sed", "more", "nl", "cut", "tr", "od", "xxd", "strings",
}
BENIGN_PROGRAMS = {"cd", "pushd", "popd", "pwd", "true", "date", "sleep"}
COMMAND_WRAPPERS = {"env", "nohup", "time", "exec", "nice", "stdbuf", "sudo", "command", "builtin"}
# Wrappers walked by _command_start to find the program they run: (short options taking a value, long options
# {name: takes a value}, positional operands before the command). Long options also match as unique prefixes.
WRAPPER_SPECS: Dict[str, Tuple[str, Dict[str, bool], int]] = {
    "env": ("uCS", {"ignore-environment": False, "null": False, "unset": True, "chdir": True, "split-string": True,
                    "block-signal": False, "default-signal": False, "ignore-signal": False,
                    "list-signal-handling": False, "debug": False, "help": False, "version": False}, 0),
    "nohup": ("", {}, 0), "command": ("", {}, 0), "builtin": ("", {}, 0), "unbuffer": ("", {}, 0),
    "exec": ("a", {}, 0),
    "time": ("fo", {"format": True, "output": True, "append": False, "verbose": False, "portability": False,
                    "quiet": False, "help": False, "version": False}, 0),
    "nice": ("n", {"adjustment": True, "help": False, "version": False}, 0),
    "stdbuf": ("ioe", {"input": True, "output": True, "error": True, "help": False, "version": False}, 0),
    "sudo": ("ugprtCDRTU", {"user": True, "group": True, "host": True, "prompt": True, "role": True, "type": True,
                            "close-from": True, "chdir": True, "chroot": True, "command-timeout": True,
                            "other-user": True, "preserve-env": False, "login": False, "shell": False,
                            "non-interactive": False, "background": False, "edit": False, "set-home": False,
                            "stdin": False, "askpass": False, "preserve-groups": False, "reset-timestamp": False,
                            "remove-timestamp": False, "list": False, "validate": False, "bell": False,
                            "help": False, "version": False}, 0),
    "doas": ("uC", {}, 0),
    "timeout": ("sk", {"signal": True, "kill-after": True, "preserve-status": False, "foreground": False,
                       "verbose": False, "help": False, "version": False}, 1),
    "setsid": ("", {"ctty": False, "fork": False, "wait": False}, 0),
    "ionice": ("cnpPu", {"class": True, "classdata": True, "pid": True, "pgid": True, "uid": True, "ignore": False},
               0),
    "taskset": ("", {"all-tasks": False, "pid": False, "cpu-list": False}, 1),
    "chroot": ("", {"userspec": True, "groups": True, "skip-chdir": False}, 1),
    "flock": ("wEc", {"timeout": True, "conflict-exit-code": True, "command": True, "shared": False,
                      "exclusive": False, "unlock": False, "nonblock": False, "close": False, "no-fork": False,
                      "verbose": False}, 1),
}
# xargs (GNU findutils 4.9 --help): short options taking a value, optional glued values, long options
# (True = takes a value, "opt" = optional value glued with '=')
XARGS_VALUE_SHORT = "adEILnPs"
XARGS_OPTIONAL_SHORT = "eil"
XARGS_LONG_OPTIONS: Dict[str, Any] = {
    "null": False, "arg-file": True, "delimiter": True, "eof": "opt", "replace": "opt", "max-lines": True,
    "max-args": True, "open-tty": False, "max-procs": True, "interactive": False, "process-slot-var": True,
    "no-run-if-empty": False, "max-chars": True, "show-limits": False, "verbose": False, "exit": False,
    "help": False, "version": False,
}
# xargs commands that only read; any other command (writers, shells, interpreters, env, wrappers) needs a confined
# producer, and even then only these may run: deleters (rm does not follow symlinks) and copiers with -t DIR
XARGS_READ_PROGRAMS = {"echo", "printf", "basename", "dirname", "realpath", "readlink", "sha1sum", "sha512sum", "du"}
XARGS_DELETE_PROGRAMS = {"rm", "rmdir", "unlink"}
XARGS_TARGET_PROGRAMS = {"cp", "mv", "ln", "install"}
# find actions that print something other than the matched path (or act on it): not a confined listing
FIND_NON_LISTING_ACTIONS = {"-printf", "-fprintf", "-fprint", "-fprint0", "-fls", "-ls", "-exec", "-execdir", "-ok",
                            "-okdir", "-delete", "-L", "-H", "-follow"}
# Shell keywords that may precede a command (if/then/do ...; `{ rm x; }`; `! cmd`)
SHELL_PREFIX_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}"}
SHELL_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "fish", "mksh", "ash"}
# Script operands that make a shell read its commands from stdin
STDIN_SCRIPT_PATHS = {"-", "/dev/stdin", "/dev/fd/0", "/proc/self/fd/0"}
# Shell script files larger than this (or unreadable / not UTF-8) cannot be judged: denied
SCRIPT_READ_LIMIT = 256 * 1024
# Bytes read first from a program invoked by path to tell a shell script (text, sh/bash/... or no shebang) apart
SCRIPT_HEAD_BYTES = 4096
# Desk shell scripts judged with relaxed rules (run-time values and unknown cwd only), pinned by the sha256 of their
# bytes: an edited copy (or any other file under scripts/) is judged strictly. Update the pin with the script.
DESK_SHELL_SCRIPTS = {
    "scripts/report_issue.sh": "9336f05e6126ec779adc868ce3bf4aec798bcacdab3ae5c5e34a8120b831390f",
}
# Fail-closed work budget of one hook evaluation (all nested lines, scripts and cwd candidates together)
AUDIT_MAX_SUBCOMMANDS = 5000
AUDIT_DEADLINE_SECONDS = 3.0
# Writers that never put new content into the files they name (chmod +x f.sh && ./f.sh runs what we read)
CONTENT_PRESERVING_WRITERS = {"chmod", "chown", "touch", "truncate", "rm", "rmdir", "unlink", "shred"}
# Builtins that assign shell variables (export X=v, declare -x X, read X, printf -v X, for X in ...)
DECLARE_BUILTINS = {"export", "declare", "typeset", "local", "readonly"}
ASSIGNING_BUILTINS = DECLARE_BUILTINS | {"read", "printf", "mapfile", "readarray", "getopts", "for", "select", "unset"}
# Interpreters whose inline code / program text may run shell commands (os.system('...'), awk system("..."))
SHELL_CALL_INTERPRETERS_RE = re.compile(r"^(?:python[0-9.]*|node|nodejs|perl|ruby|php|awk|gawk|mawk|nawk)$")
WRITE_PROGRAMS = {"cp", "mv", "rm", "tee", "truncate", "ln", "chmod", "chown", "install", "dd", "rsync",
                  "unlink", "shred", "touch"}

TRADE_ENGINE_RE = re.compile(r"\bexecute_futures_trade(?:\.py)?\b")
# Executor modes that never open a position (dispatched by the engine before the trade path)
EXECUTOR_MOVE_BREAKEVEN_FLAGS = {"--move-breakeven", "--move_breakeven"}
EXECUTOR_READ_ONLY_FLAGS = {"--positions"}
DEPLOY_BATCH_RE = re.compile(r"\bdeploy_[A-Za-z0-9_]+\.py\b")
AUTO_DEPLOY_LOOP_RE = re.compile(r"\bclimax_watcher_loop(?:\.py)?\b")
RECORD_EVALUATION_RE = re.compile(r"\brecord_evaluation(?:\.py)?\b")
USER_PROFILE_SET_RE = re.compile(r"\buser_profile(?:\.py)?\b.*\s--(?:set|setup)\b", re.IGNORECASE)

# Risk-reducing auto-allow (_subcommand_is_risk_reducing). Identity = the script a sub-command actually executes
# (_executed_script, also through python / wsl.exe), resolved lexically against the cwd (else the workspace root) and
# equal to one of these repo-relative paths: never a word anywhere in the text. Each entry: (flags of which at least
# one is required, {allowed flag: takes a value}); any other flag, an =value on a switch or a stray positional means
# "not risk-reducing" (exact spellings from each script's argparse; abbreviations are not recognised: fails closed).
HELP_FLAGS = {"--help": False, "-h": False}
EXECUTOR_RISK_FLAGS = {"--close-position", "--close_position", "--move-breakeven", "--move_breakeven",
                       "--audit-orphans", "--audit_orphans", "--auto-heal", "--auto_heal",
                       "--protect-pending", "--protect_pending"}
# --is-yolo is an opening flag, except next to --move-breakeven (stricter YOLO break-even rules, never opens)
EXECUTOR_BREAKEVEN_YOLO_FLAGS = {"--is-yolo": False, "--is_yolo": False}
# What may precede a risk-reducing script for an auto-allow: these env assignments (VAR=v, env VAR=v) and python
# options only (PYTHONPATH / PYTHONSTARTUP / -i / -m ... can make the interpreter run other code)
RISK_ENV_ASSIGNMENTS = {"BINANCE_API_ENV", "BINANCE_AUTH_MODE", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
                        "PYTHONIOENCODING", "MSYS_NO_PATHCONV"}
RISK_PYTHON_OPTIONS = {"-u", "-B", "-Xutf8"}
# Wrappers that may precede a risk-reducing script at any level for an auto-allow (issue #110): they neither change
# the user, root, shell or working directory nor feed the command from elsewhere. Any other wrapper (sudo in any
# form, doas, chroot, setsid, flock, time, ionice, taskset, exec, command, xargs, uv run ...) means "ask"
RISK_WRAPPERS = {"env", "timeout", "nice", "nohup", "stdbuf"}
# Executor --force (only acts with --move-breakeven) overrides the anti-truncation (+2.0x ATR_15m) and YOLO
# break-even-after-TP1 rules: still risk-reducing in shape (never sent to the trade gates, never denied), but a
# forced break-even always asks the user (issue #111)
FORCED_BREAKEVEN_BLOCKER = ("--force overrides the break-even rules (anti-truncation +2.0x ATR_15m, YOLO break-even "
                            "only after TP1)")
RISK_REDUCING_SCRIPTS: Dict[str, Tuple[set, Dict[str, bool]]] = {
    # --positions is allowed next to a risk flag but is not one itself (alone: read-only listing, normal policy)
    # --force is recognised but never auto-allowed (FORCED_BREAKEVEN_BLOCKER, issue #111)
    "scripts/execute_futures_trade.py": (
        EXECUTOR_RISK_FLAGS | set(HELP_FLAGS),
        {**{f: False for f in EXECUTOR_RISK_FLAGS}, "--positions": False, "--symbol": True, "--json": False,
         "--force": False, "--env": True, **EXECUTOR_BREAKEVEN_YOLO_FLAGS, **HELP_FLAGS}),
    # The guardian never opens positions: only a bounded single cycle (--once), never --interval
    "scripts/loops/position_guardian_loop.py": (
        {"--once"} | set(HELP_FLAGS),
        {"--once": False, "--env": True, "--dry-run": False, "--close-dead-alpha": False, "--json": False,
         **HELP_FLAGS}),
    "scripts/loops/night_cutoff_loop.py": (
        set(), {"--env": True, "--auto-ratchet": False, "--overnight-mode": True, **HELP_FLAGS}),
    "scripts/trading_doctor.py": ({"--heal"} | set(HELP_FLAGS), {"--env": True, "--heal": False, **HELP_FLAGS}),
    # Only their --help is risk-free
    "scripts/record_evaluation.py": (set(HELP_FLAGS), dict(HELP_FLAGS)),
    "scripts/loops/climax_watcher_loop.py": (set(HELP_FLAGS), dict(HELP_FLAGS)),
    "scripts/user_profile.py": (set(HELP_FLAGS), dict(HELP_FLAGS)),
}
# Read-only analysis scripts (issue #191): offline reports that never place, change or cancel orders (trade_outcomes:
# GET userTrades and public klines / exchangeInfo; the scorecard: local files; the simulator: public klines and
# exchangeInfo). Each entry: ({allowed flag: takes a value}, {path flag: "write" | "read"}, its own outputs). A single
# plain invocation by the exact repo path (never a linked worktree copy) with only these flags is auto-allowed when
# every path flag resolves inside logs/ and every write flag names one of its own outputs
READ_ONLY_SCRIPTS: Dict[str, Tuple[Dict[str, bool], Dict[str, str], Tuple[str, ...]]] = {
    "scripts/trade_outcomes.py": (
        {"--since": True, "--symbol": True, "--env": True, "--json": False, "--no-klines": False, "--output": True,
         **HELP_FLAGS},
        {"--output": "write"}, ("logs/trade_outcomes.jsonl",)),
    "scripts/trading_scorecard.py": (
        {"--env": True, "--json": False, "--out": True, "--outcomes": True, **HELP_FLAGS},
        {"--out": "write", "--outcomes": "read"}, ("logs/trading_scorecard.json", "logs/score_calibration.json")),
    "scripts/exit_policy_sim.py": (
        {"--env": True, "--outcomes": True, "--policies": True, "--horizon-hours": True, "--taker-fee": True,
         "--maker-fee": True, "--trail-cadence": True, "--exact-entry-only": False, "--json": False, "--out": True,
         **HELP_FLAGS},
        {"--out": "write", "--outcomes": "read"}, ("logs/exit_policy_sim.json",)),
}
# logs/ files with another sanctioned writer: a read-only script's write flag may name none of them except its own
# outputs (with GROUND_TRUTH_FILES: the desk's audit / brief / journal files and the read-only scripts' outputs)
READ_ONLY_FOREIGN_OUTPUTS = ({"logs/trades_audit.jsonl", "logs/primed_brief.json", "logs/primed_brief_scores.json",
                              "logs/guardian_actions.jsonl", "logs/trade_insights.jsonl", "logs/shadow_trades.jsonl",
                              "logs/issues_backlog.jsonl"}
                             | {p for _f, _p, outs in READ_ONLY_SCRIPTS.values() for p in outs})
READ_ONLY_ALLOW_REASON = "Authorized as a read-only analysis script ({}): it places, changes or cancels no order."
# Executor options that open (or shape the opening of) a position, matched with argparse's unique-prefix
# abbreviations (--dir, --lev): an engine sub-command naming one is judged as a trade opening, never as an exit
EXECUTOR_OPENING_OPTIONS = (
    "--direction", "--leverage", "--margin", "--trigger-price", "--trigger_price", "--sl-price", "--sl_price",
    "--tp1-price", "--tp1_price", "--tp2-price", "--tp2_price", "--order-type", "--order_type", "--limit-price",
    "--limit_price", "--bypass-eval-gate", "--bypass_eval_gate", "--bypass-delta-gate", "--bypass_delta_gate",
    "--is-yolo", "--is_yolo", "--confirmed", "--user-confirmed",
)
# Characters a risk-reducing sub-command may not carry in any token to be auto-allowed: a shell that parses the
# arguments again (wsl.exe without -e) may run them as commands; redirects (>, >>, 2>&1) write files
RISK_AUTO_ALLOW_METACHARS = (";", "|", "&", "$", "<", ">", "`", "\n", "\r")
# Programs / cmdlets that change the working directory of the rest of the line
CHDIR_PROGRAMS = {"cd", "pushd", "popd", "chdir", "set-location", "sl", "push-location", "pop-location"}
PYTHON_PROGRAM_RE = re.compile(r"^python[0-9.]*(?:\.exe)?$")
PYTHON_LONG_VALUE_OPTIONS = {"--check-hash-based-pycs"}
# Text an auto-allow cannot vouch for: the analysis may not see every command it runs (a comment or an
# apostrophe hiding the next lines, heredoc bodies, ANSI-C strings, command substitutions)
AUTO_ALLOW_BLOCKERS = (("<<", "a heredoc / here-string"), ("$'", "an ANSI-C $'...' string"),
                       ("`", "a backtick command substitution"), ("$(", "a $(...) command substitution"))

# Trading primitives that must never appear in inline code (python -c, heredocs, piped interpreters)
INLINE_TRADING_PRIMITIVES_RE = re.compile(
    r"execute_futures_trade|send_signed_request|send_mcp_gateway_request|call_binance_mcp|place_algo_stop_loss|"
    r"setup_margin_and_leverage|/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType|positionSide|positionMargin)\b|"
    r"agent\.binance\.com",
    re.IGNORECASE,
)
# Primitives that mark a script file as order-capable
SCRIPT_TRADING_PRIMITIVES_RE = re.compile(
    r"\bimport\s+execute_futures_trade\b|\bfrom\s+execute_futures_trade\s+import\b|send_signed_request|"
    r"send_mcp_gateway_request|call_binance_mcp|place_algo_stop_loss|"
    r"/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType)\b|futures_usds\.newOrder|agent\.binance\.com"
)
# Order-placing endpoints / helpers (content written by file tools)
WRITE_ENDPOINT_PRIMITIVES_RE = re.compile(
    r"/fapi/v1/(?:order|batchOrders|algoOrder|leverage|marginType)\b|place_algo_stop_loss\s*\(|"
    r"send_mcp_gateway_request\s*\(|futures_usds\.newOrder"
)
INLINE_PYTHON_RE = re.compile(r"\bpython[0-9.]*(?:\.exe)?['\"]?(?:\s+-[A-Za-z]+)*\s+-[A-Za-z]*c\b", re.IGNORECASE)
STDIN_PYTHON_RE = re.compile(r"\bpython[0-9.]*(?:\.exe)?['\"]?(?:\s+-[A-Za-z]+)*\s+-(?:\s|$)", re.IGNORECASE)
PIPE_TO_INTERPRETER_RE = re.compile(
    r"\|\s*(?:sudo\s+)?(?:python[0-9.]*|sh|bash|zsh|dash|node|perl|ruby)\b(?!\s+[^\s|;&-][^\s|;&]*\.(?:py|sh|js|pl|rb)\b)",
    re.IGNORECASE,
)
# `eval` as a shell word only (not inside flags such as --bypass-eval-gate)
OTHER_INLINE_RE = re.compile(r"\b(?:node|perl|ruby)\s+-e\b|\b(?:sh|bash|zsh)\s+-c\b|(?<![\w-])eval(?![\w-])", re.IGNORECASE)
BASE64_EXEC_RE = re.compile(r"base64\s+(?:-d|--decode|-D)\b.*\|\s*(?:python[0-9.]*|sh|bash|zsh|node|perl)\b", re.IGNORECASE)

BINANCE_HOST_RE = re.compile(
    r"(?:[a-z0-9-]*fapi[a-z0-9.-]*|[a-z0-9-]*dapi[a-z0-9.-]*|api[a-z0-9-]*|agent)\.binance\.com|binancefuture\.com",
    re.IGNORECASE,
)
HTTP_WRITE_RE = re.compile(
    r"(?:-X|--request)\s*['\"]?(?:POST|PUT|DELETE|PATCH)\b|(?:^|\s)(?:-d|--data(?:-raw|-binary|-urlencode)?|-F|--form|--json)\b|"
    r"--post-data|--post-file|--method[=\s]+['\"]?(?:POST|PUT|DELETE|PATCH)\b|\b(?:POST|PUT|DELETE|PATCH)\s+https?://",
    re.IGNORECASE,
)
HTTP_CLIENT_RE = re.compile(r"\b(?:curl|wget|http|https|xh|httpie)\b", re.IGNORECASE)

# Evaluation trail (dossiers + Antigravity brain / Claude Code subagent transcripts used for provenance)
EVALUATION_TRAIL_CMD_RE = re.compile(
    r"latest_dos|logs[\\/]+eval|\bevaluations[\\/]|\.gemini[\\/]+[^\\/\s'\"]+[\\/]+brain\b|"
    r"antigravity[^\\/\s'\"]*[\\/]+brain\b|\bsubagents[\\/]+agent-a[0-9a-f]|"
    r"\.claude[\\/]+projects[\\/]+[^\\/\s'\"]+[\\/]+[^\\/\s'\"]+[\\/]+subagents\b",
    re.IGNORECASE,
)
# Test-only overrides of the transcript roots must never reach the recorder or the executor from the agent:
# they would let a forged transcript outside the runtime's own directory sign a dossier.
TRANSCRIPT_ROOT_OVERRIDE_RE = re.compile(
    r"\b(?:AGY_BRAIN_DIRS|CLAUDE_PROJECTS_DIRS)\b(?:['\"]\]?)?\s*=|\b(?:AGY_BRAIN_DIRS|CLAUDE_PROJECTS_DIRS)['\"]\s*[,:]"
)
# Ground-truth runtime state: each file feeds the PROD order checks and has exactly one sanctioned writer, a desk
# script that writes it from Python (atomic_write_json), never through a shell command or a file tool.
# session_state.json is a cache for the hook's pre-check (defense in depth): the executor's live-anchored gates are
# authoritative. guardian_state.json is read directly by the executor (guardian liveness for resting entries).
# pending_entries.json is cross-checked against the exchange's resting orders and supplies total_qty for MCP algo
# entries listed without a quantity (Gate 1, issue #119); the hook's own max-open-positions pre-check counts its
# same-env symbols (issue #48). hook_heartbeat.json is this guard's liveness attestation (issue #73): the guard
# refreshes it from Python on every live invocation (_write_heartbeat); a forged one would make the hooks look alive.
# score_calibration.json decides whether an autonomous Tier S needs the user's confirmation (issue #202); a forged
# calibrated bucket would skip it. trade_outcomes.jsonl is the only input the scorecard merges into that store, and
# trades_audit.jsonl (the executor's entry ledger, also read by Gate 0A and the exit manager) is its source.
# primed_brief.json (the evaluator's only input, read with the Read tool, which stays allowed) and
# primed_brief_scores.json (the radar scores the recorder joins into the dossier for the calibrated-bucket gate).
GROUND_TRUTH_FILES = {
    "logs/session_state.json": "`python3 scripts/sync_session_state.py`",
    "logs/guardian_state.json": "`python3 scripts/loops/position_guardian_loop.py`",
    "logs/pending_entries.json": "`python3 scripts/execute_futures_trade.py` (resting-entry registration and --protect-pending)",
    "logs/hook_heartbeat.json": "`scripts/hooks/pre_trade_guard.py` itself (refreshed on every live hook invocation)",
    "logs/score_calibration.json": "`python3 scripts/trading_scorecard.py`",
    "logs/trade_outcomes.jsonl": "`python3 scripts/trade_outcomes.py`",
    "logs/trades_audit.jsonl": "`python3 scripts/execute_futures_trade.py` (entry audit records and failsafe-abort events)",
    "logs/primed_brief.json": "`python3 scripts/prime_evaluator_brief.py`",
    "logs/primed_brief_scores.json": "`python3 scripts/prime_evaluator_brief.py`",
}
GROUND_TRUTH_BASENAMES = {path.rsplit("/", 1)[-1].lower(): path for path in GROUND_TRUTH_FILES}
GROUND_TRUTH_RE = re.compile("|".join(re.escape(n) for n in GROUND_TRUTH_BASENAMES), re.IGNORECASE)
GROUND_TRUTH_TARGET_RE = re.compile(
    r"(?:^|/)logs/(" + "|".join(re.escape(n) for n in GROUND_TRUTH_BASENAMES) + r")$", re.IGNORECASE
)
SHELL_GLOB_RE = re.compile(r"[*?\[{]")
# Windows drive (C:\x, C:/x), UNC (\\server\share) and drive-mount (/mnt/c/x, Git Bash /c/x) path forms
WINDOWS_FORM_PATH_RE = re.compile(r"^(?:[A-Za-z]:(?:/|$)|//[^/]|/mnt/[A-Za-z](?:/|$)|/[A-Za-z]/)")
# A shell sub-command that names a protected file (literally or through a logs/ glob / brace word) is denied unless
# its program is one of these read-only tools (plus the special cases in _ground_truth_read_only).
GROUND_TRUTH_READ_PROGRAMS = {"cat", "head", "tail", "less", "more", "grep", "egrep", "rg", "jq", "wc", "stat",
                              "ls", "file", "diff", "cmp", "md5sum", "sha256sum"}
# Programs that destroy the logs/ directory itself (rm -rf logs, shred -u logs/*)
LOGS_DIR_DESTRUCTIVE_PROGRAMS = {"rm", "shred", "unlink", "truncate"}
# Programs that can overwrite files inside logs/ with sources whose names are not visible (cp -r src/. logs)
LOGS_DIR_COPY_PROGRAMS = {"cp", "rsync", "install"}
# Windows / PowerShell recursive copy programs whose destination (2nd positional) can be logs/ or an ancestor
WINDOWS_COPY_PROGRAMS = {"robocopy", "xcopy", "copy", "copy-item", "cpi"}
# robocopy / xcopy / copy switches (/E, /MIR, /XD:x); a second '/' makes it a path (/tmp/src)
WINDOWS_COPY_SWITCH_RE = re.compile(r"^/[A-Za-z0-9?]+(?::[^/\\]*)?$")
WINDOWS_RECURSIVE_SWITCHES = {"/s", "/e", "/mir", "/purge", "/mov", "/move"}
# Copy-Item parameters that take a value (other than -Path / -LiteralPath / -Destination)
PS_COPY_VALUE_PARAMS = ("filter", "include", "exclude", "credential", "fromsession", "tosession")
# Archive extractors; the destination directory is -C / -d / -o<dir> / -DestinationPath, else the cwd
ARCHIVE_EXTRACT_PROGRAMS = {"tar", "bsdtar", "unzip", "7z", "7za", "7zr", "expand-archive"}
# GNU tar (1.35 --help): short options taking a value and long options taking a value (also as the next word; tar
# accepts unique prefixes, so any prefix counts); options whose value is a shell command or a file tar writes.
TAR_VALUE_SHORT = set("fCbgFHIKLNTVX")
TAR_VALUE_LONG = ("file", "directory", "blocking-factor", "listed-incremental", "info-script", "new-volume-script",
                  "format", "use-compress-program", "starting-file", "tape-length", "newer", "after-date",
                  "files-from", "label", "exclude-from", "exclude", "add-file", "transform", "xform", "owner", "group",
                  "mode", "mtime", "to-command", "rmt-command", "rsh-command", "volno-file", "index-file",
                  "record-size", "level", "newer-mtime", "suffix", "strip-components", "hole-detection", "sort",
                  "owner-map", "group-map", "quoting-style", "quote-chars", "no-quote-chars", "pax-option",
                  "sparse-version", "warning", "exclude-tag", "exclude-tag-all", "exclude-tag-under",
                  "exclude-ignore", "exclude-ignore-recursive", "xattrs-exclude", "xattrs-include")
TAR_EXEC_LONG = ("to-command", "use-compress-program", "info-script", "new-volume-script", "rmt-command",
                 "rsh-command")
TAR_EXEC_SHORT = set("IF")
TAR_OUTPUT_LONG = ("index-file", "listed-incremental", "volno-file")
# Programs accepting -t DIR / --target-directory=DIR (GNU coreutils)
TARGET_DIR_PROGRAMS = {"cp", "mv", "install", "ln"}
# git sub-commands that never modify working-tree files (anything else naming a protected file is denied)
GIT_READ_SUBCOMMANDS = {"status", "diff", "log", "show", "grep", "blame", "commit", "add", "ls-files", "ls-tree",
                        "check-ignore", "rev-parse", "branch", "shortlog", "describe", "cat-file", "fetch", "push"}
# git sub-commands that rewrite work-tree files named in their arguments (a script named there counts as written)
GIT_WRITE_SUBCOMMANDS = {"checkout", "restore", "apply", "mv", "rm", "reset", "stash", "merge", "pull", "am",
                         "cherry-pick", "revert", "rebase", "switch", "worktree", "clone", "archive", "init"}
GIT_GLOBAL_VALUE_OPTIONS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
# Options that turn an allowlisted read into a write or a command run (git log --output=F, git grep -O<cmd>,
# git -c core.fsmonitor=<cmd>, rg --pre CMD, less -o F); leading VAR= assignments (LESSOPEN, GIT_*) too.
GIT_RUN_GLOBAL_OPTIONS = ("--config-env", "--exec-path")
# Sub-command long options that write a file or run a command -> shortest abbreviation counted. git's parse-options
# accepts any unique prefix (--op= for --open-files-in-pager, --upl=, --rece=, --exe=), so every prefix counts
# (config-env from 4 letters: --co/--con abbreviate --contains/--color...). Longer spellings count too (--output-*).
GIT_RUN_LONG_OPTIONS = {"output": 1, "open-files-in-pager": 1, "ext-diff": 1, "upload-pack": 1, "receive-pack": 1,
                        "exec": 1, "exec-path": 1, "config-env": 4, "extcmd": 1}
# ...of which these run a shell command (their value, or the pager on matched files); the last need a value
GIT_EXEC_LONG_OPTIONS = ("open-files-in-pager", "upload-pack", "receive-pack", "exec", "extcmd")
GIT_EXEC_VALUE_OPTIONS = ("upload-pack", "receive-pack", "exec", "extcmd")
# Short aliases of command-running git options, per sub-command (git 2.43 usage): their value is a nested command
# and, for local-repo/file operations, follows the same operand rule as the long form.
#   clone / ls-remote -u = --upload-pack ; rebase -x = --exec ; difftool -x = --extcmd
GIT_SUBCOMMAND_SHORT_EXEC = {"clone": {"u"}, "ls-remote": {"u"}, "rebase": {"x"}, "difftool": {"x"}}
# fetch / pull -u is --update-head-ok (no value) in git 2.43; older docs list it as --upload-pack: the next word is
# judged as a command too, but still parsed as an operand (fail-safe for both readings).
GIT_SUBCOMMAND_SHORT_EXEC_MAYBE = {"fetch": {"u"}, "pull": {"u"}}
# Short clusters with O (git grep -O<cmd>, -nOrm; diff -O<orderfile>) or, for diff/log/show, o void the exemption
GIT_LOWER_O_SUBCOMMANDS = {"diff", "log", "show"}
# git grep short options that take a value (git 2.43 `git grep -h`: -A -B -C -e -f -m), so -eOrder = -e Order (NOT
# -O) and -m1 / -A2 are glued values: once one appears in a short cluster, the rest of the cluster is its value.
# -O is handled separately (optional attached pager value).
GIT_GREP_VALUE_SHORT = set("ABCefm")
GIT_GREP_PATTERN_OPTS_SHORT = set("ef")           # a pattern given via -e/-f: all positionals are then paths
# git grep long options (True = takes a value, also as the next word; "opt" = optional value glued with '='). git
# accepts any unique prefix (--thr 2 = --threads 2, --max-d 2 = --max-depth 2) and --no-<option>.
GIT_GREP_LONG_OPTIONS: Dict[str, Any] = {
    "cached": False, "no-index": False, "index": False, "untracked": False, "exclude-standard": False,
    "recurse-submodules": False, "invert-match": False, "ignore-case": False, "word-regexp": False, "text": False,
    "textconv": False, "recursive": False, "max-depth": True, "extended-regexp": False, "basic-regexp": False,
    "fixed-strings": False, "perl-regexp": False, "line-number": False, "column": False, "full-name": False,
    "files-with-matches": False, "name-only": False, "files-without-match": False, "null": False,
    "only-matching": False, "count": False, "color": "opt", "break": False, "heading": False, "context": True,
    "before-context": True, "after-context": True, "threads": True, "show-function": False,
    "function-context": False, "and": False, "or": False, "not": False, "quiet": False, "all-match": False,
    "open-files-in-pager": "opt", "ext-grep": False, "max-count": True,
}
RG_EXEC_OPTIONS = ("--pre", "--hostname-bin")
# rg options that take a value (ripgrep 15 --help), skipped when collecting the operands --pre / --hostname-bin act
# on. rg does not accept abbreviated long options.
RG_VALUE_SHORT = set("efEmjgdtTABCMr")
RG_VALUE_LONG = {"regexp", "file", "pre", "pre-glob", "dfa-size-limit", "encoding", "engine", "max-count",
                 "regex-size-limit", "threads", "glob", "iglob", "ignore-file", "cursor-ignore", "max-depth",
                 "max-filesize", "type", "type-not", "type-add", "type-clear", "after-context", "before-context",
                 "color", "colors", "context", "context-separator", "field-context-separator",
                 "field-match-separator", "hostname-bin", "hyperlink-format", "max-columns", "path-separator",
                 "replace", "sort", "sortr", "generate"}
ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")
ENV_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)(\+?)=(.*)$", re.DOTALL)
# Value of a variable assigned at run time (read X, for X in ..., declare -n): never a literal
UNKNOWN_VALUE = "$__unknown__"
# Env vars denied outright on ANY command, whatever their value: config / startup-file / library injection, git
# repository redirection, and command channels that run their value (git diff, ssh, askpass, less preprocessors).
ENV_DENY_OUTRIGHT = {"RIPGREP_CONFIG_PATH", "GIT_CONFIG_PARAMETERS", "GIT_CONFIG_COUNT", "GIT_CONFIG_GLOBAL",
                     "GIT_CONFIG_SYSTEM", "GIT_EXEC_PATH", "GIT_TEMPLATE_DIR", "GIT_DIR", "GIT_WORK_TREE",
                     "GIT_COMMON_DIR", "GIT_INDEX_FILE", "PROMPT_COMMAND", "LD_PRELOAD", "LD_AUDIT",
                     "LD_LIBRARY_PATH", "GIT_EXTERNAL_DIFF", "GIT_SSH", "GIT_SSH_COMMAND", "GIT_ASKPASS",
                     "SSH_ASKPASS", "GIT_PROXY_COMMAND", "LESSOPEN", "LESSCLOSE", "GIT_ALLOW_PROTOCOL",
                     "GIT_PROTOCOL_FROM_USER"}
ENV_DENY_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
# Startup files sourced by a shell: denied when the program is a shell / env / a script, or when exported
ENV_STARTUP_VARS = {"ENV", "BASH_ENV"}
# Pager / editor command channels: only an allowlisted literal value is accepted (cat / less / more with read-only
# flags, or empty; true / : / cat for editors). LESS (less options) follows the same less-flag rule.
ENV_PAGER_VARS = {"GIT_PAGER", "PAGER", "MANPAGER"}
ENV_EDITOR_VARS = {"EDITOR", "VISUAL", "GIT_EDITOR", "GIT_SEQUENCE_EDITOR"}
ENV_PROTECTED_VARS = ENV_DENY_OUTRIGHT | ENV_STARTUP_VARS | ENV_PAGER_VARS | ENV_EDITOR_VARS | {"LESS"}
PAGER_PROGRAMS = {"cat", "less", "more"}
EDITOR_VALUES = {"true", ":", "cat"}
SYSTEM_BIN_DIRS = ("/bin/", "/usr/bin/")
GIT_BOOLEAN_VALUES = {"true", "false", "yes", "no", "on", "off", "1", "0"}
# Git config keys that hold a command or a path to an executable / config / hooks / work tree. Pager and editor
# keys take the allowlisted values above; every other one is denied whatever its value: `git config` persisting
# it, `git -c key=value`, `git clone -c`, `git --config-env=<key>=<envvar>` (hidden value: denied for all of them).
GIT_CONFIG_DANGEROUS_RE = re.compile(
    r"^(?:core\.(?:pager|editor|sshcommand|askpass|fsmonitor|hookspath|gitproxy|worktree|alternaterefscommand)|"
    r"pager\..+|sequence\.editor|interactive\.difffilter|diff\.external|diff\..+\.(?:textconv|command)|"
    r"difftool\..+\.(?:cmd|path)|merge\..+\.driver|mergetool\..+\.(?:cmd|path)|"
    r"filter\..+\.(?:clean|smudge|process)|remote\..+\.(?:uploadpack|receivepack|proxy)|alias\..+|"
    r"gpg\.program|gpg\..+\.program|gpg\.ssh\.defaultkeycommand|credential\.helper|credential\..+\.helper|"
    r"uploadpack\.packobjectshook|include\.path|includeif\..+\.path|init\.templatedir|submodule\..+\.update|"
    r"trailer\..+\.(?:command|cmd)|hook\..+\.command|browser\..+\.(?:cmd|path)|man\..+\.(?:cmd|path)|"
    r"sendemail\..*(?:smtpserver|cmd)|protocol\.(?:.+\.)?allow)$", re.IGNORECASE)
# protocol.allow / protocol.<name>.allow (ext:: runs a shell command as transport): only `never` is accepted
GIT_CONFIG_PROTOCOL_KEY_RE = re.compile(r"^protocol\.(?:.+\.)?allow$", re.IGNORECASE)
GIT_CONFIG_PAGER_KEY_RE = re.compile(r"^(?:core\.pager|pager\..+)$", re.IGNORECASE)
GIT_CONFIG_EDITOR_KEY_RE = re.compile(r"^(?:core\.editor|sequence\.editor)$", re.IGNORECASE)
# `git config` options (git 2.43 usage + the get/set/unset/list/... sub-command syntax): True = takes a value
GIT_CONFIG_LONG_OPTIONS: Dict[str, bool] = {
    "global": False, "system": False, "local": False, "worktree": False, "file": True, "blob": True, "get": False,
    "get-all": False, "get-regexp": False, "get-urlmatch": False, "replace-all": False, "add": False,
    "unset": False, "unset-all": False, "rename-section": False, "remove-section": False, "list": False,
    "fixed-value": False, "edit": False, "get-color": False, "get-colorbool": False, "type": True, "bool": False,
    "int": False, "bool-or-int": False, "bool-or-str": False, "path": False, "expiry-date": False, "null": False,
    "name-only": False, "includes": False, "show-origin": False, "show-scope": False, "default": True,
    "comment": True, "value": True, "all": False, "regexp": False, "url": True, "append": False,
}
GIT_CONFIG_VALUE_SHORT = set("ft")
# A `git config` call is a read only with an explicit read action
GIT_CONFIG_READ_ACTIONS = {"get", "get-all", "get-regexp", "get-urlmatch", "list", "get-color", "get-colorbool"}
GIT_CONFIG_SUBCOMMANDS = {"get", "set", "unset", "list", "rename-section", "remove-section", "edit"}
# Output-file values of any program (dd of=F, --output=F, --log-file=F) and per-program output options
OUTPUT_VALUE_RE = re.compile(
    r"^(?:of=|--(?:output[\w-]*|log-file|target-directory|directory|files-from|index-file|listed-incremental)=)",
    re.IGNORECASE)
OUTPUT_OPTIONS = {
    "curl": {"-o", "--output"}, "wget": {"-O", "--output-document", "-o", "--output-file", "-a", "--append-output"},
    "sort": {"-o", "--output"}, "less": {"-o", "-O", "--log-file", "--LOG-FILE"}, "unzip": {"-d"},
    "tar": {"-f", "--file", "-C", "--directory", "-g", "--listed-incremental", "--index-file"},
    "bsdtar": {"-f", "--file", "-C", "--directory"}, "make": {"-C"}, "time": {"-o", "--output"},
}
# Windows / PowerShell programs that delete or move directories (cmd //c rd /s /q logs, Remove-Item -Recurse logs)
WINDOWS_DELETE_PROGRAMS = {"rd", "rmdir", "del", "erase", "remove-item", "ri"}
WINDOWS_MOVE_PROGRAMS = {"move", "ren", "rename", "move-item", "mi", "rename-item", "rni"}
WINDOWS_SWITCH_RE = re.compile(r"^/[A-Za-z?](?::\S*)?$")
SHELL_VALUE_OPTIONS = {"-o", "+o", "-O", "+O", "--rcfile", "--init-file"}
NESTED_DEPTH_LIMIT = 6
# Command substitutions that expand to the working directory / repo root, or that prefix a path ($(x)/logs)
CWD_SUBSTITUTION_RE = re.compile(
    r"\$\(\s*(?:pwd(?:\s+-[LP])?|git\s+rev-parse\s+--show-toplevel)\s*\)|"
    r"`\s*(?:pwd(?:\s+-[LP])?|git\s+rev-parse\s+--show-toplevel)\s*`|\$\{PWD\}|\$PWD(?![A-Za-z0-9_])"
)
HOME_VAR_RE = re.compile(r"\$\{HOME\}|\$HOME(?![A-Za-z0-9_])")
PATH_SUBSTITUTION_RE = re.compile(r"\$\(([^()\n]*)\)(?=/)|`([^`\n]*)`(?=/)")
COMMAND_SUBSTITUTION_RE = re.compile(r"\$\(([^()\n]*)\)|`([^`\n]*)`")
FIND_DELETE_ACTIONS = {"-delete"}
FIND_OUTPUT_ACTIONS = {"-fprint", "-fprint0", "-fprintf", "-fls"}
FIND_EXEC_ACTIONS = {"-exec", "-execdir", "-ok", "-okdir"}
FIND_NAME_FILTERS = {"-name", "-iname"}
FIND_PATH_FILTERS = {"-path", "-ipath", "-wholename", "-iwholename"}
# Heredoc bodies are dropped from the line-by-line check only when they feed python/node (judged by markers)
CODE_INTERPRETER_PROGRAM_RE = re.compile(r"^(?:python[0-9.]*|node|nodejs)$", re.IGNORECASE)
PIPE_TO_CODE_INTERPRETER_RE = re.compile(
    r"^[^|;&\n]*\|\s*(?:python[0-9.]*|node)\b(?!\s+[^\s|;&-][^\s|;&]*\.(?:py|js)\b)", re.IGNORECASE)
HARNESS_PATH_CMD_RE = re.compile(
    r"scripts[\\/]+hooks[\\/]|\.agents[\\/]+hooks\.json|dossier_provenance\.py|record_evaluation\.py|"
    r"\.agents[\\/]+agents[\\/]|\.claude[\\/]+agents[\\/]|\.claude[\\/]+settings|config[\\/]+user_profile\.json|"
    r"scripts[\\/]+report_issue\.sh|"
    # Gate modules never run as programs (issue #79): path and bare basename
    r"(?<![\w-])(?:gate_limits|portfolio_exposure|env_resolver|score_calibration)\.py",
    re.IGNORECASE,
)
# Gate modules that are also run as programs (issue #79): only a write target counts, never the program being run
# (python3 scripts/execute_futures_trade.py --close-position 2>&1 keeps its decision)
GATE_PROGRAM_PATH_RE = re.compile(r"scripts[\\/]+(?:execute_futures_trade|user_profile)\.py", re.IGNORECASE)
# A whole token that is such a path (relative, ./, absolute or drive form): the script operand of an interpreter
GATE_PROGRAM_TOKEN_RE = re.compile(r"(?:[^\s'\"();|&<>]*[\\/])?scripts[\\/]+(?:execute_futures_trade|user_profile)\.py",
                                   re.IGNORECASE)
# Python interpreters / launcher that run a gate module in PowerShell (python, python3.12, python.exe, py)
PS_PYTHON_RUNNER_RE = re.compile(r"^(?:python[0-9.]*|py)(?:\.exe)?$")
HARNESS_WRITE_REASON = ("Command modifies trading harness / gate modules (hooks, dossier provenance, profile, "
                        "executor gates). Explicit confirmation required.")
# Git config / hook files a later, innocent git command executes (core.fsmonitor, hooks, aliases, include.path):
# matched on the normalised path (forward slashes, no drive, '.' / '..' collapsed, lower case) whatever the root, so
# ./.git/config, /abs/repo/.git/hooks/pre-commit, ~/.gitconfig and $HOME/.config/git/config all match. A bare .git
# is a gitdir file in a worktree / submodule (gitdir: <path>): rewriting it redirects git to another repository.
# .gitignore, .gitattributes, .gitmodules, .github/ and .git/info/ (exclude, attributes, sparse-checkout: they run
# nothing by themselves) do not match.
GIT_EXEC_CONFIG_PATH_RE = re.compile(
    r"(?:^|/)(?:\.git(?:/(?:config(?:\.worktree)?|hooks(?:/.*)?|"
    r"worktrees/[^/]+(?:/(?:config[^/]*|commondir|gitdir))?|"
    r"modules/(?:.+/)?(?:config(?:\.worktree)?|hooks(?:/.*)?)))?|"
    r"\.gitconfig[^/]*|\.config/git/config|etc/gitconfig)$", re.IGNORECASE)
# Pure deletions plant no payload in a git config / hook file
GIT_EXEC_CONFIG_DELETE_PROGRAMS = {"rm", "rmdir", "unlink", "shred", "rd", "del", "erase", "remove-item", "ri"}
# Programs whose write operands are modelled one by one (or that only print / navigate / run nested lines judged on
# their own): the catch-all (any other program naming a git config / hook path) leaves them to those rules, so
# `cp .git/config /tmp/x` and `mv .git/hooks/x /tmp` stay allowed. git itself is judged by the git config key rules.
GIT_EXEC_CONFIG_MODELLED_PROGRAMS = {"cp", "mv", "tee", "truncate", "chmod", "chown", "install", "dd", "rsync",
                                     "touch", "sed", "tar", "bsdtar", "unzip", "7z", "7za", "7zr", "robocopy",
                                     "xcopy", "copy", "copy-item", "cpi", "move", "ren", "rename", "move-item", "mi",
                                     "rename-item", "rni", "git", "wsl", "echo", "printf", "true", ":", "test", "[",
                                     "basename", "dirname", "realpath", "readlink"}
# Link options of cp (-l / -s / --link / --symbolic-link) and rsync (--link-dest=DIR): a link aliases its SOURCE,
# so every operand counts (ln -s .git/config x; echo y >> x)
CP_LINK_LONG_OPTIONS = ("--link", "--symbolic-link")
# Extra read-only programs for the git catch-all (xxd only without -r / -revert, which writes its second operand)
GIT_EXEC_CONFIG_READ_PROGRAMS = {"du", "tree", "od", "hexdump", "nl", "xxd"}
# Env vars that move where git reads its global config from (HOME/.gitconfig, $XDG_CONFIG_HOME/git/config): denied
# in front of git like GIT_DIR / GIT_CONFIG_GLOBAL
GIT_HOME_ENV_VARS = {"HOME", "XDG_CONFIG_HOME"}
# Path-like words inside an argument (perl -e 'open(F,q(>>.git/config))', vim -c 'w .git/config')
GIT_PATH_WORD_RE = re.compile(r"[^\s'\"();,=<>|&`{}\[\]]+")
# Pseudo hit key (next to the GROUND_TRUTH_FILES keys) for a shell write to a git config / hook file
GIT_EXEC_CONFIG_KEY = "__git_exec_config__"
GIT_EXEC_CONFIG_REASON = (
    "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Git Config Channel Protection): Commands must not write git config or hook "
    "files (.git/config, .git/config.worktree, .git/hooks/, .git/worktrees/*/config, "
    ".git/modules/*/config|hooks, a .git gitdir file, ~/.gitconfig, ~/.config/git/config, /etc/gitconfig): a "
    "later git command runs what they configure (core.fsmonitor, hooks, aliases, include.path); nor point git at "
    "another config or repository (HOME= / XDG_CONFIG_HOME= in front of git, git --git-dir). Use `git config "
    "<key> <value>` (judged by the git config key rules) or the file tools (explicit confirmation)."
)
INLINE_WRITE_MARKERS_RE = re.compile(
    r"\.write\s*\(|write_text\s*\(|write_bytes\s*\(|dump\s*\(|open\s*\([^)]*['\"][rwabxt+]*[wax+][rwabxt+]*['\"]|"
    r"os\.(?:remove|unlink|replace|rename|truncate|system|popen|open|symlink|link)\b|\.unlink\s*\(|\.rename\s*\(|"
    r"\.replace\s*\(|\.touch\s*\(|\.(?:symlink|hardlink|link)_to\s*\(|\bO_(?:WRONLY|RDWR|CREAT|TRUNC|APPEND)\b|"
    r"FileIO\s*\(|shutil\.|subprocess\.|\.system\s*\(|\.popen\s*\(|\bexec\s*\(|__import__|"
    r"\bos\.(?:exec|spawn|posix_spawn)\w*|\bpty\.|"
    r"writeFileSync|writeFile\s*\(|unlinkSync|rmSync|openSync|"
    r"(?:\bfs|require\s*\(\s*['\"](?:node:)?fs(?:/promises)?['\"]\s*\))\.(?:rm|rmdir|write\w*|open\w*)\s*\(|"
    r"\b(?:rm|rmdir|rmdirSync|writeSync)\s*\(|"
    r"\b(?:appendFile|copyFile|rename|symlink|truncate|cp)(?:Sync)?\s*\(|createWriteStream|"
    # Perl (File::Path, bareword list operators) and Ruby (FileUtils, File, Dir)
    r"\bremove_tree\b|\brmtree\b|\brename\b|\bunlink\b|\bremove_entry\b|\bremove_dir\b|"
    r"\bFileUtils\.\w+|\bFile\.(?:delete|rename|unlink)\b|\bDir\.(?:rmdir|delete|unlink)\b",
    re.IGNORECASE,
)
# Inline code acting on the logs/ directory itself (shutil.rmtree('logs'), fs.rmSync('logs', {recursive: true})):
# the destructive markers apply to a 'logs' string literal (or a logs/ glob reaching a protected file); the strong
# markers also apply to literals naming an ancestor of logs/ ('.', '..', '/', the repo root) or a glob ('*', 'log*').
INLINE_STRING_LITERAL_RE = re.compile(r"(['\"])([^'\"\s]*)\1")
INLINE_LOGS_DIR_MARKERS_RE = re.compile(
    r"rmtree|removedirs|remove_tree|\brename\w*\s*\(|\brename\b|\.replace\s*\(|\bremove\s*\(|unlink|"
    r"\brm(?:dir)?(?:Sync)?\s*\(|rmSync|symlink|\.(?:hardlink|symlink)_to\s*\(|\blink(?:Sync)?\s*\(|"
    r"shutil\.(?:move|copytree)|\bcp(?:Sync)?\s*\(|copyFile|"
    r"\bFileUtils\.\w+|\bFile\.(?:delete|rename)\b|\bDir\.(?:rmdir|delete)\b|\bremove_entry\b|\bremove_dir\b",
    re.IGNORECASE,
)
# Inline-code calls that hand a string to a shell (their string literals are judged as nested command lines)
INLINE_SHELL_CALL_RE = re.compile(
    r"(?:\b(?:os\.)?(?:system|popen[234]?|spawn[lvpe]*|exec[lvpe]+|posix_spawnp?|getoutput|getstatusoutput)|"
    r"\bsubprocess\.\w+|\b(?:check_output|check_call|Popen|execSync|execFileSync|spawnSync|execFile|exec|spawn)|"
    r"\bIO\.popen|\bOpen3\.\w+)\s*[(\[{]?",
    re.IGNORECASE,
)
INLINE_QX_RE = re.compile(r"(?:%x|\bqx)\s*([({\[/|!])(.*?)[)}\]/|!]", re.DOTALL)
INLINE_ANCESTOR_MARKERS_RE = re.compile(
    r"rmtree|removedirs|remove_tree|rmSync|\brm\s*\([^)]*recursive|shutil\.move|\brename(?:s|Sync)?\s*\(|\brename\b|"
    r"\bFileUtils\.(?:rm_rf|rm_r|remove_dir|remove_entry|mv)\b", re.IGNORECASE
)

# File-tool targets
HARNESS_FILES = {
    ".agents/hooks.json", "scripts/utils/dossier_provenance.py", "scripts/record_evaluation.py",
    ".claude/settings.json", ".claude/settings.local.json", "config/user_profile.json", "scripts/report_issue.sh",
    # Gate modules (issue #79): PROD gate values, gate classification, env resolution, leverage ceiling
    "scripts/utils/gate_limits.py", "scripts/execute_futures_trade.py", "scripts/utils/portfolio_exposure.py",
    "scripts/utils/env_resolver.py", "scripts/user_profile.py",
    # Issue #202: decides when an autonomous Tier S needs the user's confirmation
    "scripts/utils/score_calibration.py",
}
HARNESS_DIRS = ("scripts/hooks/", ".agents/agents/", ".claude/agents/")
BRAIN_PATH_RE = re.compile(r"(?:^|/)\.gemini/[^/]+/brain(?:/|$)", re.IGNORECASE)
# File-tool target inside a logs/evaluations/ directory (relative, POSIX, WSL or Windows absolute)
EVALUATION_TRAIL_TARGET_RE = re.compile(r"(?:^|/)logs/evaluations(?:/|$)", re.IGNORECASE)
# Claude Code subagent transcripts: <projects>/<slug>/<sessionId>/subagents/agent-<id>.jsonl (+ .meta.json)
CLAUDE_SUBAGENT_PATH_RE = re.compile(r"(?:^|/)subagents/agent-a[0-9a-f]+\.(?:jsonl|meta\.json)$|"
                                     r"(?:^|/)\.claude/projects/[^/]+/[^/]+/subagents(?:/|$)", re.IGNORECASE)

# -----------------------------------------------------------------------------
# PowerShell tool (Claude Code on Windows). Commands are normalised for analysis only (backtick escapes stripped,
# backslashes -> '/') and judged like Bash, plus a backstop for commands naming a protected target.
# -----------------------------------------------------------------------------
# PowerShell accepts typographic quotes and dashes as quotes / parameter dashes
PS_UNICODE_TRANSLATION = str.maketrans({
    "‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-",
})
# Nested bodies ({...}, $(...), @(...), (...)) are judged on their own; the statement keeps this neutral word
PS_NESTED_PLACEHOLDER = "__ps_nested__"
PS_OPENERS = {"(": ")", "{": "}"}
PS_SCAN_DEPTH_LIMIT = 32
# '#' / '<#' start a comment only at the start of a token
PS_TOKEN_BOUNDARY = " \t\r\n;|&(){}"
PS_DECISION_RANK = {"allow": 0, "ask": 1, "force_ask": 2, "deny": 3}
# Expressions (not commands) at the start of a statement: $var, $_.Length, $env:X, .is_valid
PS_EXPRESSION_LEAD_RE = re.compile(r"^(?:\$[A-Za-z_?][\w:?]*|\.[A-Za-z_]\w*)(?:\.[A-Za-z_]\w*)*$")
PS_ASSIGNMENT_OPERATORS = ("??=", "+=", "-=", "*=", "/=", "%=", "=")
# wsl.exe [options] [--|-e] <linux command>: the command is judged with the Bash ground-truth logic
WSL_VALUE_OPTIONS = {"-d", "--distribution", "-u", "--user", "--cd", "--shell-type"}
WSL_COMMAND_OPTIONS = {"--", "-e", "--exec"}
# Read-only cmdlets / aliases allowed to name protected files (also added to the ground-truth read programs)
PS_READ_CMDLETS = frozenset({
    "get-content", "gc", "cat", "type", "select-string", "sls", "test-path", "get-item", "gi", "get-childitem",
    "gci", "ls", "dir", "get-filehash", "measure-object", "measure", "convertfrom-json", "convertto-json",
    "select-object", "select", "where-object", "where", "?", "sort-object", "sort", "format-table", "ft",
    "format-list", "fl", "out-string", "write-output", "echo", "write-host", "get-date", "join-path", "split-path",
    "resolve-path", "set-location", "sl", "cd", "push-location", "pushd", "pop-location", "popd", "get-location",
    "gl", "pwd", "if", "elseif", "else",
})
_PS_WORD_START, _PS_WORD_END = r"(?<![\w.$-])", r"(?![\w-])"
# Write / destructive cmdlets and aliases, anywhere in the command (script blocks and pipelines included)
PS_WRITE_CMDLET_RE = re.compile(
    _PS_WORD_START + r"(?:set-content|sc|add-content|ac|out-file|clear-content|clc|new-item|ni|md|mkdir|"
    r"copy-item|cp|cpi|copy|move-item|mv|mi|move|rename-item|ren|rni|remove-item|rm|ri|del|erase|rd|rmdir|"
    r"tee-object|tee|set-item|si|set-itemproperty|sp|clear-item|cli|clear-itemproperty|clp|new-itemproperty|"
    r"remove-itemproperty|rp|copy-itemproperty|cpp|move-itemproperty|mp|rename-itemproperty|rnp|set-acl|"
    r"export-\w+|epcsv|invoke-webrequest|iwr|invoke-restmethod|irm|start-bitstransfer|expand-archive|"
    r"compress-archive)" + _PS_WORD_END,
    re.IGNORECASE,
)
# Cmdlets that run arbitrary code or programs
PS_EXEC_CMDLET_RE = re.compile(
    _PS_WORD_START + r"(?:invoke-expression|iex|invoke-command|icm|start-process|saps|start|start-job|sajb|"
    r"invoke-item|ii|foreach-object|foreach)" + _PS_WORD_END,
    re.IGNORECASE,
)
PS_DOTNET_STATIC_RE = re.compile(r"\[[^\]\n]+\]\s*::")
PS_METHOD_CALL_RE = re.compile(r"\.\s*[A-Za-z_]\w*\s*\(")
# Provider variables write files: ${C:\repo\logs\x.json} = '...'
PS_PROVIDER_VARIABLE_RE = re.compile(r"\$\{[^}]*[:/]")
# Call operator / dot-sourcing / % (ForEach-Object) at the start of a statement (& $cmd, . ./x.ps1, | % Delete)
PS_CALL_OPERATOR_RE = re.compile(r"(?:^|[;|{(\n]|&&|\|\|)\s*(?:&(?![&>])|\.(?=\s)|%(?=\s|\{|$))")
# NTFS 8.3 short name inside logs/ (logs\SESSIO~1.JSO is an alias of a protected file)
PS_SHORT_NAME_RE = re.compile(r"(?:^|/)logs/[^/]*~\d", re.IGNORECASE)
PS_NAMED_VALUE_RE = re.compile(r"^-[A-Za-z][\w-]*:")
# Unauditable payloads: powershell/pwsh -EncodedCommand (-e, -ec, -en..., -ea) or FromBase64String executed
PS_ENCODED_COMMAND_RE = re.compile(
    _PS_WORD_START + r"(?:powershell|pwsh)(?:\.exe)?" + _PS_WORD_END
    + r"[^\n;|]*?(?<=[\s'\",])(?:--?|/)(?:e|ec|ea|en[a-z]*)(?=[\s'\",:]|$)",
    re.IGNORECASE,
)
# Raw HTTP writes through the PowerShell web cmdlets (Invoke-RestMethod -Method Post ... fapi.binance.com)
PS_HTTP_CMDLET_RE = re.compile(
    _PS_WORD_START + r"(?:invoke-restmethod|irm|invoke-webrequest|iwr)" + _PS_WORD_END, re.IGNORECASE
)
PS_HTTP_WRITE_RE = re.compile(r"-method[:\s]+['\"]?(?:post|put|delete|patch)\b|-body\b|-infile\b", re.IGNORECASE)
PS_FROM_BASE64_RE = re.compile(r"frombase64string", re.IGNORECASE)
# PowerShell inline code (the equivalent of `| bash` / `bash -c`): iex / Invoke-Expression anywhere, a pipe into
# powershell / pwsh, [scriptblock]::Create
PS_INLINE_CODE_RE = re.compile(
    _PS_WORD_START + r"(?:iex|invoke-expression)" + _PS_WORD_END
    + r"|\|\s*(?:\S*/)?(?:powershell|pwsh)(?:\.exe)?(?![\w-])|scriptblock\]\s*::\s*create",
    re.IGNORECASE,
)
PS_BASE64_RUNNER_RE = re.compile(
    _PS_WORD_START + r"(?:iex|invoke-expression|invoke-command|icm|powershell|pwsh)" + _PS_WORD_END
    + r"|&|scriptblock\]\s*::\s*create|invokescript|newscriptblock|\.invoke\s*\(",
    re.IGNORECASE,
)


# =============================================================================
# Generic decoding helpers
# =============================================================================
def _decode_value(value: Any, max_depth: int = 3) -> Any:
    """agy may JSON-encode tool-call argument values (e.g. ServerName='"binance"'). Unwraps them."""
    for _ in range(max_depth):
        if not isinstance(value, str):
            return value
        stripped = value.strip()
        if not stripped or stripped[0] not in "\"{[":
            return value
        try:
            value = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            return value
    return value


def _decode_str(value: Any) -> str:
    value = _decode_value(value)
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _decode_dict(value: Any) -> dict:
    value = _decode_value(value)
    return value if isinstance(value, dict) else {}


def _first(d: dict, *keys: str) -> Any:
    for k in keys:
        if isinstance(d, dict) and k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _is_true(value: Any) -> bool:
    value = _decode_value(value)
    return value is True or str(value).strip().lower() == "true"


def _arg_truthy(value: Any) -> bool:
    """Same truthiness as execute_futures_trade._truthy (True, 'true', '1', 'yes'); used to mirror executor flags."""
    value = _decode_value(value)
    return value is True or str(value).strip().lower() in ("true", "1", "yes")


# argparse accepts prefixes of --is-yolo / --is_yolo (--is, --is-, --is_y...); the executor treats them as YOLO.
# They are the only executor options starting with --is (tests pin this against the executor's argparse).
IS_YOLO_FLAG_RE = re.compile(r"(?<![\w-])--is(?:[-_][a-z]*)?(?![\w-])", re.IGNORECASE)
# Executor confirmation flags. Only the exact tokens count: abbreviations argparse would also accept are deliberately
# not recognised (a missed confirmation denies, which fails closed).
EXECUTOR_CONFIRM_OPTIONS = ("--confirmed", "--user-confirmed")
CONFIRM_RE_RUN_HINT = "👉 Ask the user to confirm in chat and re-run with the '--confirmed' flag."


def parse_mcp_arguments(args_dict: dict) -> dict:
    """Extracts dictionary of arguments from tool call args regardless of serialization."""
    if not args_dict:
        return {}
    if "Arguments" in args_dict:
        return _decode_dict(args_dict["Arguments"])
    return args_dict


# =============================================================================
# Tool call normalization (agy / Claude Code)
# =============================================================================
def _split_eager_mcp_name(name: str, args: dict) -> Tuple[str, str]:
    """Splits agy eager MCP names `mcp_<server>_<tool>` (server names may contain underscores)."""
    rest = name[len("mcp_"):]
    explicit_server = _decode_str(_first(args, "ServerName", "serverName", "server_name", "server"))
    if explicit_server and rest.startswith(explicit_server + "_"):
        return explicit_server, rest[len(explicit_server) + 1:]
    for server in sorted(KNOWN_MCP_SERVERS, key=len, reverse=True):
        if rest.startswith(server + "_"):
            return server, rest[len(server) + 1:]
    if "_" in rest:
        server, tool = rest.split("_", 1)
        return server, tool
    return explicit_server, rest


def normalize_tool_call(payload: dict) -> Dict[str, Any]:
    """
    Maps agy and Claude Code payloads onto a common structure:
    {kind: run_command|mcp|file_write|other, tool, command, cwd, shell, server, mcp_tool, mcp_args, raw_args,
     target_file, content}
    shell is "powershell" for the Claude Code PowerShell tool (command = raw PowerShell text), otherwise "bash".
    """
    call: Dict[str, Any] = {
        "kind": "other", "tool": "", "command": "", "cwd": "", "shell": "bash", "server": "", "mcp_tool": "",
        "mcp_args": {}, "raw_args": {}, "target_file": "", "content": "",
    }

    if isinstance(payload.get("toolCall"), dict):
        tool_call = payload["toolCall"]
        name = tool_call.get("name", "")
        args = tool_call.get("args", {})
        if not isinstance(args, dict):
            args = _decode_dict(args)
        call["tool"] = name
        call["raw_args"] = args

        if name == "run_command":
            call["kind"] = "run_command"
            call["command"] = _decode_str(_first(args, "CommandLine", "commandLine", "command"))
            call["cwd"] = _decode_str(_first(args, "Cwd", "cwd"))
        elif name in AGY_MCP_CALL_TOOLS:
            call["kind"] = "mcp"
            call["server"] = _decode_str(_first(args, "ServerName", "serverName", "server_name", "server"))
            call["mcp_tool"] = _decode_str(_first(args, "ToolName", "toolName", "tool_name", "tool"))
            call["mcp_args"] = _decode_dict(_first(args, "Arguments", "arguments", "Args", "input"))
        elif name.startswith("mcp_"):
            call["kind"] = "mcp"
            server, tool = _split_eager_mcp_name(name, args)
            call["server"], call["mcp_tool"] = server, tool
            nested = _first(args, "Arguments", "arguments")
            call["mcp_args"] = _decode_dict(nested) if nested is not None else {
                k: _decode_value(v) for k, v in args.items()
                if k not in ("ServerName", "serverName", "toolAction", "toolSummary")
            }
        elif BINANCE_NAMESPACE_RE.match(name) or BINANCE_VERB_RE.match(name):
            # Eagerly loaded Binance tool called by its bare name
            call["kind"] = "mcp"
            call["server"] = "binance"
            call["mcp_tool"] = name
            nested = _first(args, "Arguments", "arguments")
            call["mcp_args"] = _decode_dict(nested) if nested is not None else {k: _decode_value(v) for k, v in args.items()}
        elif name in FILE_WRITE_TOOLS:
            call["kind"] = "file_write"
            call["target_file"] = _decode_str(_first(args, "TargetFile", "targetFile", "AbsolutePath", "file_path", "path"))
            call["content"] = json.dumps(args, ensure_ascii=False)
        return call

    # Claude Code PreToolUse contract
    name = payload.get("tool_name", "")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        tool_input = {}
    call["tool"] = name
    call["raw_args"] = tool_input
    if isinstance(name, str) and name in CLAUDE_SHELL_TOOLS:
        call["kind"] = "run_command"
        call["shell"] = CLAUDE_SHELL_TOOLS[name]
        call["command"] = _decode_str(tool_input.get("command"))
        call["cwd"] = _decode_str(payload.get("cwd"))
    elif isinstance(name, str) and name.startswith("mcp__"):
        parts = name.split("__")
        call["kind"] = "mcp"
        call["server"] = parts[1] if len(parts) > 1 else ""
        call["mcp_tool"] = "__".join(parts[2:]) if len(parts) > 2 else ""
        call["mcp_args"] = {k: _decode_value(v) for k, v in tool_input.items()}
    elif name in CLAUDE_FILE_WRITE_TOOLS:
        call["kind"] = "file_write"
        call["target_file"] = _decode_str(_first(tool_input, "file_path", "notebook_path", "path"))
        call["content"] = json.dumps(tool_input, ensure_ascii=False)
    return call


# =============================================================================
# Argument extraction helpers
# =============================================================================
def is_risk_reducing_action(cmd_or_name: str, args_dict: dict = None, cwd: str = "", base_dir: str = "") -> bool:
    """
    Verifies whether an action reduces or eliminates risk (NEVER blocked).
    Structured arguments ONLY:
    - MCP: reduceOnly=true, closePosition=true, or cancel / delete operations
    - CLI (one sub-command): the executed script is one of RISK_REDUCING_SCRIPTS by its exact repo path (resolved
      against cwd, else base_dir / the workspace root) with only that script's exclusive flag set (docstring 4):
      e.g. `execute_futures_trade.py --close-position --symbol X`, `--move-breakeven` with exactly one --symbol,
      `position_guardian_loop.py --once`, `trading_doctor.py --heal`. Batch deploy scripts are never
      risk-reducing.
    Never relies on generic substring 'close' or a script name anywhere in the command line.
    """
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        if _is_true(_first(mcp_args, "reduceOnly", "reduce_only", "reduce-only")):
            return True
        if _is_true(_first(mcp_args, "closePosition", "close_position", "close-position")):
            return True
        mcp_tool = _decode_str(args_dict.get("ToolName", "")).lower()
        op = mcp_tool.rsplit(".", 1)[-1]
        if "cancel" in op or op.startswith("delete"):
            return True

    if cmd_or_name:
        return _subcommand_is_risk_reducing(_tokenize_subcommand(cmd_or_name), cmd_or_name, cwd, base_dir)
    return False


def extract_target_symbol(cmd: str, args_dict: dict = None) -> str:
    """Extracts target symbol from tool arguments or shell command line."""
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        if "symbol" in mcp_args and mcp_args["symbol"]:
            return str(_decode_value(mcp_args["symbol"])).upper().strip()
        if "symbol" in args_dict and args_dict["symbol"]:
            return str(_decode_value(args_dict["symbol"])).upper().strip()

    if cmd:
        symbols = {m.group(2).upper().strip() for m in re.finditer(r"--symbol(?:\s+|=)(['\"]?)([A-Za-z0-9_]+)\1", cmd)}
        if len(symbols) == 1:
            return symbols.pop()
        if len(symbols) > 1:
            return ""
        m2 = re.search(r"['\"]([A-Z0-9]+USDT)['\"]", cmd)
        if m2:
            return m2.group(1).upper().strip()
    return ""


def extract_env_argument(cmd: str, args_dict: dict = None) -> Optional[str]:
    """
    Extracts explicit environment parameter from tool args or command line.
    Shell assignments (BINANCE_API_ENV=prod, export BINANCE_API_ENV=prod) are honored as well.
    If several values are present and any of them is PROD, PROD wins (fail-safe).
    """
    found: List[str] = []
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        for key in ["target_env", "env", "TARGET_ENV", "BINANCE_API_ENV"]:
            if key in args_dict and args_dict[key] and not isinstance(args_dict[key], (dict, list)):
                found.append(_decode_str(args_dict[key]).strip())
            if key in mcp_args and mcp_args[key] and not isinstance(mcp_args[key], (dict, list)):
                found.append(_decode_str(mcp_args[key]).strip())

    if cmd:
        for m in re.finditer(r"--env(?:\s+|=)(['\"]?)([A-Za-z0-9_-]+)\1", cmd, re.IGNORECASE):
            found.append(m.group(2).strip())
        for m in re.finditer(r"\b(?:BINANCE_API_ENV|TARGET_ENV)=(['\"]?)([A-Za-z0-9_-]+)\1", cmd):
            found.append(m.group(2).strip())

    if not found:
        return None
    for val in found:
        if val.lower() in ("prod", "production", "mainnet"):
            return val
    return found[0]


def parse_trade_direction(cmd: str, args_dict: dict = None) -> Tuple[Optional[str], Optional[str]]:
    """
    Strictly parses trade direction ('LONG' or 'SHORT').
    Returns (direction, error_reason).
    If both or neither match, returns (None, error_reason) for fail-closed rejection.
    """
    found_dirs = set()

    # 1. Check structured MCP arguments
    if args_dict:
        mcp_args = parse_mcp_arguments(args_dict)
        dir_val = _decode_str(mcp_args.get("direction", "")).upper().strip()
        side_val = _decode_str(mcp_args.get("side", "")).upper().strip()

        if dir_val in ["LONG", "BUY"]:
            found_dirs.add("LONG")
        elif dir_val in ["SHORT", "SELL"]:
            found_dirs.add("SHORT")
        elif dir_val:
            return None, f"Invalid direction value '{dir_val}' in tool arguments."

        if side_val in ["BUY", "LONG"]:
            found_dirs.add("LONG")
        elif side_val in ["SELL", "SHORT"]:
            found_dirs.add("SHORT")
        elif side_val:
            return None, f"Invalid side value '{side_val}' in tool arguments."

        if len(found_dirs) == 1:
            return next(iter(found_dirs)), None
        elif len(found_dirs) > 1:
            return None, "Conflicting trade directions detected in tool arguments (both LONG and SHORT)."

    # 2. Check structured command line flags: --dir, --direction, --side
    if cmd:
        flags_found = set()
        for m in re.finditer(r"--(?:dir(?:ection)?|side)(?:\s+|=)(['\"]?)(LONG|SHORT|BUY|SELL)\1\b", cmd, re.IGNORECASE):
            val = m.group(2).upper()
            if val in ["LONG", "BUY"]:
                flags_found.add("LONG")
            elif val in ["SHORT", "SELL"]:
                flags_found.add("SHORT")

        if not flags_found:
            # Check for quoted standalone words 'LONG' or 'SHORT' (e.g. positional script args)
            quoted_long = bool(re.search(r"['\"](LONG|BUY)['\"]", cmd, re.IGNORECASE))
            quoted_short = bool(re.search(r"['\"](SHORT|SELL)['\"]", cmd, re.IGNORECASE))
            if quoted_long:
                flags_found.add("LONG")
            if quoted_short:
                flags_found.add("SHORT")

        if len(flags_found) == 1:
            return next(iter(flags_found)), None
        elif len(flags_found) > 1:
            return None, "Conflicting trade directions detected in command line (both LONG and SHORT matched)."

    return None, "Unable to determine trade direction strictly (neither LONG nor SHORT found)."


# =============================================================================
# Dossier gate (single source of truth: scripts/utils/dossier_provenance.py)
# =============================================================================
def check_dossier(symbol: str, direction: Optional[str], env: str, base_dir: str,
                  conversation_id: Optional[str] = None, now_ts: Optional[int] = None) -> Tuple[bool, str, Optional[dict]]:
    """Validates the evaluator dossier for a trade and, in PROD, binds it to the current conversation."""
    if dp is None:
        return False, "Dossier provenance module (scripts/utils/dossier_provenance.py) is unavailable.", None
    ok, reason, cand = dp.validate_dossier_for_trade(
        symbol, direction, env, base_dir=base_dir, now_ts=now_ts if now_ts is not None else int(time.time())
    )
    if not ok:
        return False, reason, None

    if env != "prod":
        return True, reason, cand

    # PROD: the provenance hash only binds the raw <dossier_json> block. Re-derive the verdict from the
    # evaluator transcript itself so a tampered approved list / parent id in the JSON file cannot widen it.
    try:
        record = dp.load_dossier(dp.default_dossier_path(base_dir))
        rebuilt = dp.build_record_from_extraction(dp.extract_recorded_transcript(record))
    except Exception as e:
        return False, f"Failed to re-derive the dossier from the evaluator transcript ({e}).", None
    rebuilt_cand = dp.find_candidate(rebuilt, symbol) if rebuilt.get("status") == "APPROVED" else None
    if rebuilt_cand is None or (direction and rebuilt_cand.get("direction") != str(direction).upper()):
        return False, (
            f"Evaluator transcript does not approve {symbol} {direction or ''}".rstrip()
            + " (the recorded dossier differs from what the evaluator emitted)."
        ), None
    # The validated provenance sha (issue #202): binds the radar snapshot the calibration gate reads to this dossier
    cand = dict(rebuilt_cand, dossier_sha256=(rebuilt.get("provenance") or {}).get("sha256"))

    if conversation_id:
        parent = rebuilt.get("parent_conversation_id")
        if parent and str(parent) != str(conversation_id):
            return False, (
                f"Evaluation dossier was produced for conversation '{parent}', not for the current "
                f"conversation '{conversation_id}'. Re-run the evaluator from this conversation."
            ), None
    return True, reason, cand


def _candidate_is_yolo(cand: Optional[dict], truthy=_is_true) -> bool:
    """`truthy=_arg_truthy` matches the executor's _dossier_candidate_is_yolo (stricter for confirmation gates);
    the default keeps the narrower notion where YOLO status *authorizes* more leverage."""
    if not isinstance(cand, dict):
        return False
    tier = str(cand.get("tier", "")).lower()
    strategy = str(cand.get("strategy", "")).lower()
    return truthy(cand.get("is_yolo")) or "yolo" in tier or "yolo" in strategy


def leverage_limits(user_prof: dict) -> Tuple[int, int, int]:
    """
    Returns (standard, ceiling, yolo_cap), aligned with execute_futures_trade.check_mechanical_gates:
    the absolute ceiling comes from user_profile.get_leverage_ceiling() (default 15x, max 125x) and the
    YOLO cap from profile `leverage_yolo`, clamped to [1, ceiling].
    """
    user_prof = user_prof or {}
    try:
        import user_profile as up
        ceiling = int(up.get_leverage_ceiling(user_prof))
    except Exception:
        ceiling = 15
    try:
        std_lev = int(user_prof.get("leverage_standard", 3))
    except (TypeError, ValueError):
        std_lev = 3
    try:
        yolo_cap = int(user_prof.get("leverage_yolo", ceiling))
    except (TypeError, ValueError):
        yolo_cap = ceiling
    yolo_cap = min(max(yolo_cap, 1), ceiling)
    return min(std_lev, ceiling), ceiling, yolo_cap


def check_leverage_bounds(requested_leverage: int, user_prof: dict, is_yolo: bool) -> Optional[str]:
    """Absolute ceiling / YOLO cap check. Returns a denial reason or None."""
    std_lev, ceiling, yolo_cap = leverage_limits(user_prof)
    if requested_leverage < 1:
        return f"Invalid leverage {requested_leverage}x. Must be >= 1x."
    if requested_leverage > ceiling:
        return f"Leverage {requested_leverage}x exceeds absolute desk ceiling of {ceiling}x (profile leverage_ceiling)."
    if (is_yolo or requested_leverage > std_lev) and requested_leverage > yolo_cap:
        return f"Leverage {requested_leverage}x exceeds the YOLO leverage limit ({yolo_cap}x, profile leverage_yolo)."
    return None


def check_leverage_gate(symbol: str, requested_leverage: int, base_dir: str, user_prof: dict = None,
                        target_env: Optional[str] = None, conversation_id: Optional[str] = None) -> Tuple[bool, str]:
    """
    Validates leverage changes against the profile limits (leverage_standard, leverage_yolo and the absolute
    ceiling from user_profile.get_leverage_ceiling()) and the approved YOLO status in the dossier.
    """
    if user_prof is None:
        try:
            import user_profile as up
            user_prof = up.load_user_profile(base_dir=base_dir)
        except Exception:
            user_prof = {}

    prefix = "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): "
    bounds_err = check_leverage_bounds(requested_leverage, user_prof, is_yolo=False)
    if bounds_err:
        return False, prefix + bounds_err

    std_lev, _ceiling, _yolo_cap = leverage_limits(user_prof)
    if requested_leverage <= std_lev:
        return True, f"Standard leverage (<= {std_lev}x) authorized."

    env = resolve_env(target_env, base_dir=base_dir)
    dossier_file = os.path.join(base_dir, "logs", "evaluations", "latest_dossier.json")
    if not os.path.exists(dossier_file):
        return False, prefix + f"Requested leverage ({requested_leverage}x > {std_lev}x) exceeds standard ceiling and no evaluation dossier exists."

    # A leverage change has no direction: bind it to the direction approved for the symbol.
    cand_direction = None
    try:
        record = dp.load_dossier(dossier_file) if dp is not None else {}
        cand_direction = (dp.find_candidate(record, symbol) or {}).get("direction") if dp is not None else None
    except Exception as e:
        return False, prefix + f"Failed to read evaluation dossier ({e})."

    ok, reason, cand = check_dossier(symbol, cand_direction, env, base_dir, conversation_id)
    if not ok:
        return False, prefix + reason

    is_yolo_authorized = _candidate_is_yolo(cand)
    if cand and not is_yolo_authorized:
        try:
            is_yolo_authorized = int(cand.get("leverage", 3)) >= requested_leverage
        except (TypeError, ValueError):
            is_yolo_authorized = False
    try:
        record = dp.load_dossier(dossier_file)
        if symbol.upper() in [str(s).upper() for s in record.get("yolo_approved_symbols", []) or []]:
            is_yolo_authorized = True
    except Exception:
        pass

    if not is_yolo_authorized:
        return False, prefix + (
            f"Requested leverage ({requested_leverage}x > {std_lev}x) for '{symbol}' is not authorized as a YOLO moonshot in the evaluation dossier."
        )

    if not user_prof.get("yolo_slot_enabled", False):
        return False, prefix + f"Requested leverage ({requested_leverage}x > {std_lev}x) requires YOLO status, but YOLO moonshot slot is disabled in user profile."

    return True, f"YOLO moonshot leverage ({requested_leverage}x) authorized for '{symbol}' in evaluation dossier."


# =============================================================================
# Shell command analysis
# =============================================================================
class AuditBudgetExceeded(Exception):
    """The ground-truth analysis of one hook evaluation exceeded AUDIT_MAX_SUBCOMMANDS or AUDIT_DEADLINE_SECONDS."""


# One audit scope per hook evaluation (shared by every analyze_run_command call, nested line, script and cwd
# candidate): sub-command counter, monotonic deadline and the memo of judged lines / scripts.
_AUDIT: Dict[str, Any] = {"active": 0, "count": 0, "deadline": 0.0, "memo": {}}
AUDIT_BUDGET_REASON = (
    "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): the command is too complex to audit (more than "
    f"{AUDIT_MAX_SUBCOMMANDS} sub-command analyses or {AUDIT_DEADLINE_SECONDS:g}s of nested commands, scripts and "
    "working directories). Split it into simpler commands."
)


@contextlib.contextmanager
def _audit_scope():
    """Opens the audit scope of a hook evaluation (re-entrant: an inner scope shares the outer budget)."""
    if not _AUDIT["active"]:
        _AUDIT.update(count=0, deadline=time.monotonic() + AUDIT_DEADLINE_SECONDS, memo={})
    _AUDIT["active"] += 1
    try:
        yield
    finally:
        _AUDIT["active"] -= 1
        if not _AUDIT["active"]:
            _AUDIT["memo"] = {}


def _audit_tick() -> None:
    """Counts one sub-command analysis; raises AuditBudgetExceeded past the budget (callers deny: fail closed)."""
    if not _AUDIT["active"]:
        return
    _AUDIT["count"] += 1
    if _AUDIT["count"] > AUDIT_MAX_SUBCOMMANDS or time.monotonic() > _AUDIT["deadline"]:
        raise AuditBudgetExceeded()


def _split_operators(tok: str) -> List[str]:
    """Splits a shlex punctuation run into bash operators: ')>' -> ')', '>'; ';>' -> ';', '>'; '&&\\n' -> '&&', '\\n'."""
    if not tok or any(c not in SHELL_PUNCTUATION for c in tok):
        return [tok]
    out: List[str] = []
    i = 0
    while i < len(tok):
        op = next((o for o in SHELL_OPERATORS if tok.startswith(o, i)), tok[i])
        out.append(op)
        i += len(op)
    return out


def _shlex_tokens(text: str) -> List[str]:
    lexer = shlex.shlex(text, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [part for tok in lexer for part in _split_operators(tok)]


class ShellScanError(ValueError):
    """Unexpected input for the Bash lexical pre-pass (_scan_shell); never caught by the tokenizer, so the hook's
    top-level handler denies (fail closed)."""


def _dq_escape(text: str) -> str:
    """Text placed inside a "..." string for shlex: backslashes and double quotes escaped, line breaks as
    QUOTED_NEWLINE_SENTINEL (the token keeps the original characters)."""
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", QUOTED_NEWLINE_SENTINEL)


def _sq_unescape_dq(text: str) -> str:
    """Inverse of the quoting part of _dq_escape (heads / tails of a heredoc operator inside "$(...)")."""
    return re.sub(r'\\(["\\])', r"\1", text)


def _ansi_c_decode(raw: str) -> str:
    """Value of a bash $'...' string body: \\n \\t \\e ... \\nnn (octal), \\xHH, \\uHHHH, \\UHHHHHHHH, \\cX and \\' \\"
    \\\\ decoded, an unknown escape kept with its backslash; NUL bytes are dropped (bash would cut the string there,
    keeping more text is the strict direction)."""
    out: List[str] = []
    i, n = 0, len(raw)
    while i < n:
        c = raw[i]
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = raw[i + 1]
        if d in ANSI_C_ESCAPES:
            out.append(ANSI_C_ESCAPES[d])
            i += 2
        elif d in "01234567":
            m = re.match(r"[0-7]{1,3}", raw[i + 1:])
            out.append(chr(int(m.group(0), 8) & 0xFF))
            i += 1 + len(m.group(0))
        elif d in "xuU":
            m = re.match(r"[0-9A-Fa-f]{1,%d}" % {"x": 2, "u": 4, "U": 8}[d], raw[i + 2:])
            if not m:
                out.append(raw[i:i + 2])
                i += 2
                continue
            try:
                out.append(chr(int(m.group(0), 16)))
            except (ValueError, OverflowError):
                pass
            i += 2 + len(m.group(0))
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(raw[i + 2]) & 0x1F))
            i += 3
        else:
            out.append(raw[i:i + 2])
            i += 2
    return "".join(out).replace("\x00", "")


def _heredoc_word(text: str, i: int) -> Optional[Tuple[int, str, bool, bool]]:
    """Parses the heredoc operator at text[i] ('<<' or '<<-', then optional blanks and a shell word: EOF, 'EOF',
    "EOF", \\EOF, E"O"F, $'EOF', $"EOF"). Returns (index after the word, delimiter after quote removal, quoted, strip tabs) or None
    when no word follows (a bash syntax error: the text is then scanned as ordinary command text)."""
    n = len(text)
    j = i + 2
    strip = j < n and text[j] == "-"
    j += 1 if strip else 0
    while j < n and text[j] in " \t":
        j += 1
    start = j
    delim: List[str] = []
    quoted = False
    while j < n and text[j] not in WORD_BOUNDARY_CHARS:
        ch = text[j]
        if ch == "$" and text.startswith("$'", j):
            k = j + 2
            while k < n and text[k] != "'":
                k += 2 if text[k] == "\\" else 1
            if k >= n:
                return None
            delim.append(_ansi_c_decode(text[j + 2:k]))
            quoted, j = True, k + 1
        elif ch == "$" and text.startswith('$"', j):
            j += 1  # $"..." (locale string): the "..." branch reads it
        elif ch == "'":
            k = text.find("'", j + 1)
            if k < 0:
                return None
            delim.append(text[j + 1:k])
            quoted, j = True, k + 1
        elif ch == '"':
            k = j + 1
            part: List[str] = []
            while k < n and text[k] != '"':
                if text[k] == "\\" and k + 1 < n and text[k + 1] in '"\\$`':
                    k += 1
                part.append(text[k])
                k += 1
            if k >= n:
                return None
            delim.append("".join(part))
            quoted, j = True, k + 1
        elif ch == "\\" and j + 1 < n:
            delim.append(text[j + 1])
            quoted, j = True, j + 2
        else:
            delim.append(ch)
            j += 1
    if j == start:
        return None
    return j, "".join(delim), quoted, strip


def _scan_frame(kind: str, parent: Optional[Dict[str, Any]], start: int, closes: bool = False, paren: int = 0,
                arith: bool = False, subst: bool = False) -> Dict[str, Any]:
    """A lexical context of _scan_shell: "cmd" (the top level or a $(...) / ((...)) body), "sq" '...', "dq" "...",
    "ansi" $'...', "bt" `...`. esc: some enclosing context is a "..." string, so the text is emitted escaped for
    shlex (one argument, as bash passes it); start: index in the output buffer where it opened; subst: a $(...)
    body; arith: a $((...)) / ((...)) body (<< is a shift there, never a heredoc)."""
    esc = bool(parent) and (parent["esc"] or parent["kind"] == "dq")
    return {"kind": kind, "esc": esc, "closes": closes, "paren": paren, "arith": arith, "subst": subst,
            "shift": False, "start": start, "line_start": start, "raw": []}


def _reject_case_in_subst(run: str, word_start: bool, before: str) -> None:
    """Raises ShellScanError when the plain text run of a $(...) body holds `case` in a command position (start of
    the body or after ; & | ( { ! newline, then, do, else, elif): its pattern `)` closes the substitution early for
    _scan_shell. `case` as an argument (git log --grep case) or in quotes / heredoc bodies is not affected."""
    for m in re.finditer(r"case(?=[ \t\r\n;&|()<>]|$)", run):
        k = m.start()
        if not ((k == 0 and word_start) or (k > 0 and run[k - 1] in WORD_BOUNDARY_CHARS)):
            continue
        prev = (before + run[:k]).rstrip(" \t")
        if not prev or prev[-1] in ";&|(\n{!" or re.search(r"(?:^|[\s;&|(])(?:then|do|else|elif|time)$", prev):
            raise ShellScanError("a case command inside $(...) cannot be audited")


def _scan_shell(text: str) -> Dict[str, Any]:
    """Bash-aware lexical pre-pass shared by the tokenizer and the auto-allow check (issue #98). Models '...',
    "..." (backslash escapes), $'...' (backslash escapes; decoded), `...`, $(...) / $((...)) / ((...)) bodies (a
    new command context, also inside "..."), unquoted backslash escapes, the backslash-newline line continuation
    (removed; the word-start state before it is kept, so `ls \\<LF># x` is a comment), comments (# at a word start
    outside quotes, to the end of the line: removed, the line break kept) and heredoc operators outside quotes and
    comments (<<WORD, <<-WORD, <<'WORD', <<"WORD", <<\\WORD, several per line with their bodies in order; <<< is a
    here-string). Returns:
      pieces: [("code", text) | ("body", heredoc)] in order. Code is the command text outside the heredoc bodies
        (the operator line stays, body and terminator line are cut out) with the line breaks inside quotes as
        QUOTED_NEWLINE_SENTINEL and $'...' rewritten as $'<decoded value>' for shlex;
      open_at: None, or the index in the last code piece of the start of the line where an unterminated quote
        opens (bash runs the complete lines before it, then fails): from there the line breaks are real again;
      deferred: heredocs inside a "...$(...)" string (their body stays inside that argument), judged as body
        lines after the text unless a plain `cat` reads them for an argument of git / gh (a commit / PR message);
      has_comment: a comment was removed.
    A heredoc is {delim, quoted, strip (<<-, leading tabs removed from the body), terminated (unterminated: the
    body runs to the end of the text, as in bash), body, head / tail (the operator line's command text before the
    operator / after the word; inside $(...) a line starting with `EOF)` also ends it), interpreter (a terminated
    body fed to python / node, _heredoc_feeds_interpreter)}. << inside $((...)) / ((...)) is a shift, never a
    heredoc. Raise ShellScanError (deny): more than HEREDOC_MAX_PER_SCAN operators, a `case` command inside $(...)
    (its pattern `)` would close the substitution early) and a multi-line arithmetic holding <<."""
    pieces: List[Tuple[str, Any]] = []
    deferred: List[Dict[str, Any]] = []
    buf: List[str] = []
    stack: List[Dict[str, Any]] = [_scan_frame("cmd", None, 0)]
    pending: List[Dict[str, Any]] = []
    has_comment = False
    word_start = True
    heredoc_count = 0
    i, n = 0, len(text)

    def emit(s: str, frame: Dict[str, Any]) -> None:
        if frame["esc"]:
            buf.append(_dq_escape(s))
        elif frame["kind"] in ("sq", "dq"):
            buf.append(s.replace("\n", QUOTED_NEWLINE_SENTINEL))
        else:
            buf.append(s)

    def push(kind: str, opener: str, parent: Dict[str, Any], **kw: Any) -> None:
        start = len(buf)
        emit(opener, parent)
        stack.append(_scan_frame(kind, parent, start, **kw))
        stack[-1]["line_start"] = len(buf)

    def read_bodies(pos: int, nl: int) -> int:
        cur = stack[-1]
        in_subst = any(fr["subst"] for fr in stack)
        for h in pending:  # tails first: a flushed buffer would lose them
            tail = "".join(buf[h["end"]:nl])
            h["tail"] = _sq_unescape_dq(tail) if h["esc"] else tail
        for h in pending:
            start = pos
            lines: List[str] = []
            terminated = False
            while pos < n:
                e = text.find("\n", pos)
                raw_line = text[pos:n if e < 0 else e]
                line = raw_line.lstrip("\t") if h["strip"] else raw_line
                if line.rstrip("\r") == h["delim"]:
                    terminated, pos = True, (n if e < 0 else e + 1)
                    break
                if in_subst and line.startswith(h["delim"] + ")"):
                    # bash ends a heredoc inside $(...) at `EOF)` too: scanning resumes at that `)`
                    terminated = True
                    pos += len(raw_line) - len(line) + len(h["delim"])
                    break
                pos = n if e < 0 else e + 1
                lines.append(line)
            h.update(body="\n".join(lines), terminated=terminated)
            # an unterminated body fed to python / node is judged too (fail closed)
            h["interpreter"] = terminated and _heredoc_feeds_interpreter(h["head"], h["tail"])
            h["shell"] = _heredoc_feeds_shell(h["head"], h["tail"])
            if cur["esc"]:
                # "$(cat <<'EOF' ... EOF)": the body is part of the quoted argument (a commit message)
                buf.append(_dq_escape(text[start:pos]))
                if not h["interpreter"] and not _heredoc_is_message(h["head"], h["tail"], h["outer"]):
                    h["shell"] = True  # its output feeds an outer command (eval "$(cat <<EOF ...)"): fail closed
                    deferred.append(h)
            else:
                pieces.append(("code", "".join(buf)))
                pieces.append(("body", h))
                buf.clear()
                for fr in stack:
                    fr["line_start"] = 0
        pending.clear()
        return pos

    while i < n:
        f = stack[-1]
        kind = f["kind"]
        c = text[i]
        if kind == "sq":
            j = text.find("'", i)
            if j < 0:
                emit(text[i:], f)
                i = n
                continue
            emit(text[i:j], f)
            stack.pop()
            emit("'", stack[-1])
            word_start, i = False, j + 1
            continue
        if kind == "ansi":
            if c == "\\" and i + 1 < n:
                f["raw"].append(text[i:i + 2])
                i += 2
            elif c == "'":
                stack.pop()
                raw = "".join(f["raw"])
                if f["esc"]:
                    buf.append(_dq_escape("$'" + raw + "'"))
                else:
                    value = _ansi_c_decode(raw).replace("'", "'\"'\"'").replace("\n", QUOTED_NEWLINE_SENTINEL)
                    buf.append("$'" + value + "'")
                word_start, i = False, i + 1
            else:
                f["raw"].append(c)
                i += 1
            continue
        if kind == "bt":
            # bash finds the closing backquote textually (backslash escapes only): quotes inside cannot pair with
            # quotes outside, so they are escaped for shlex
            if c == "\\" and i + 1 < n:
                buf.append(_dq_escape(text[i:i + 2]) if f["esc"] else text[i:i + 2])
                i += 2
            elif c == "`":
                stack.pop()
                emit("`", stack[-1])
                word_start, i = False, i + 1
            else:
                buf.append(_dq_escape(c) if f["esc"] else ("\\" + c if c in "'\"" else c))
                i += 1
            continue
        if kind == "dq":
            plain = SCAN_PLAIN_DQ_RE.match(text, i)
            if plain:
                emit(plain.group(0), f)
                i = plain.end()
            elif c == "\\" and i + 1 < n:
                if text[i + 1] != "\n":  # backslash-newline is a line continuation inside "..." too
                    emit(text[i:i + 2], f)
                i += 2
            elif c == '"':
                stack.pop()
                emit('"', stack[-1])
                word_start, i = False, i + 1
            elif text.startswith("$((", i):
                push("cmd", "$((", f, closes=True, paren=1, arith=True)
                word_start, i = True, i + 3
            elif text.startswith("$(", i):
                push("cmd", "$(", f, closes=True, subst=True)
                word_start, i = True, i + 2
            elif c == "`":
                push("bt", "`", f)
                i += 1
            else:
                emit(c, f)
                i += 1
            continue
        # command context (top level, $(...), ((...)))
        plain = SCAN_PLAIN_CMD_RE.match(text, i)
        if plain:
            if f["subst"] and "case" in plain.group(0):
                _reject_case_in_subst(plain.group(0), word_start, "".join(buf[f["line_start"]:]))
            emit(plain.group(0), f)
            word_start, i = plain.group(0)[-1] in WORD_BOUNDARY_CHARS, plain.end()
            continue
        if c == "\\":
            if i + 1 < n and text[i + 1] == "\n":
                i += 2  # line continuation: removed, word_start unchanged
                continue
            emit(text[i:i + 2], f)
            word_start, i = False, i + 2
        elif c in "'\"":
            push("sq" if c == "'" else "dq", c, f)
            word_start, i = False, i + 1
        elif text.startswith("$'", i):
            stack.append(_scan_frame("ansi", f, len(buf)))
            word_start, i = False, i + 2
        elif text.startswith("$((", i):
            push("cmd", "$((", f, closes=True, paren=1, arith=True)
            word_start, i = True, i + 3
        elif text.startswith("$(", i):
            push("cmd", "$(", f, closes=True, subst=True)
            word_start, i = True, i + 2
        elif c == "`":
            push("bt", "`", f)
            word_start, i = False, i + 1
        elif c == "(" and word_start and text.startswith("((", i):
            push("cmd", "((", f, closes=True, paren=1, arith=True)
            stack[-1]["bare"] = True
            word_start, i = True, i + 2
        elif c == ")" and f["closes"] and f["paren"] == 0:
            stack.pop()
            emit(")", stack[-1])
            # after a bare ((...)) command a new word starts (`((1))# x` is a comment); $(...) is part of a word
            word_start, i = bool(f.get("bare")), i + 1
        elif c in "()":
            if f["closes"]:
                f["paren"] += 1 if c == "(" else -1
            emit(c, f)
            word_start, i = True, i + 1
        elif c == "#" and word_start:
            j = text.find("\n", i)
            has_comment, i = True, (n if j < 0 else j)
        elif text.startswith("<<<", i):
            emit("<<<", f)
            word_start, i = True, i + 3
        elif f["arith"] and text.startswith("<<", i):
            f["shift"] = True  # a shift; see the line-break check below
            emit("<<", f)
            word_start, i = True, i + 2
        elif text.startswith("<<", i) and _heredoc_word(text, i) is not None:
            end, delim, quoted, strip = _heredoc_word(text, i)
            heredoc_count += 1
            if heredoc_count > HEREDOC_MAX_PER_SCAN:
                raise ShellScanError(f"more than {HEREDOC_MAX_PER_SCAN} heredoc operators")
            head = "".join(buf[f["line_start"]:])
            # inside "...$(...)": the command line holding that string (git commit -m "$(cat <<EOF ...)")
            first_dq = next((k for k, fr in enumerate(stack) if fr["kind"] == "dq"), None)
            outer = "".join(buf[stack[first_dq - 1]["line_start"]:]) if first_dq else ""
            emit(text[i:end], f)
            pending.append({"delim": delim, "quoted": quoted, "strip": strip, "end": len(buf), "esc": f["esc"],
                            "head": _sq_unescape_dq(head) if f["esc"] else head, "outer": outer,
                            "tail": "", "body": "", "terminated": False, "interpreter": False})
            word_start, i = False, end
        elif c == "\n":
            if f["arith"] and f["shift"]:
                # `$((cat <<EOF` is a $( (subshell) ) whose heredoc bash reads; a multi-line arithmetic with << is
                # too ambiguous to audit
                raise ShellScanError("<< in a multi-line $((...)) / ((...))")
            emit("\n", f)
            nl = len(buf) - 1
            f["line_start"] = len(buf)
            word_start, i = True, i + 1
            if pending:
                i = read_bodies(i, nl)
        else:
            emit(c, f)
            word_start, i = c in WORD_BOUNDARY_CHARS, i + 1

    if stack[-1]["kind"] == "ansi":  # unterminated $'...': kept raw
        raw = "$'" + "".join(stack[-1]["raw"])
        buf.append(_dq_escape(raw) if stack[-1]["esc"] else raw)
    code = "".join(buf)
    open_at = None
    opened = next((fr for fr in stack if fr["kind"] in ("sq", "dq", "ansi", "bt")), None)
    if opened is not None:
        start = len("".join(buf[:opened["start"]]))
        open_at = code.rfind("\n", 0, start) + 1
        code = code[:open_at] + code[open_at:].replace(QUOTED_NEWLINE_SENTINEL, "\n")
    pieces.append(("code", code))
    return {"pieces": pieces, "open_at": open_at, "deferred": deferred, "has_comment": has_comment}


def _protect_quoted_newlines(command_line: str) -> str:
    """The command text _scan_shell hands to shlex (heredoc bodies cut out, comments and line continuations
    removed): line breaks INSIDE a '...' / "..." / $'...' string are QUOTED_NEWLINE_SENTINEL, a line break between
    a closing quote and the next line's opening quote stays a command separator. From the line where an
    unterminated quote opens (an apostrophe in a word, unbalanced text) the line breaks stay real: over-splitting is
    the fail-safe direction."""
    return "".join(value for kind, value in _scan_shell(command_line)["pieces"] if kind == "code")


def _auto_allow_blocker(command_line: str) -> Optional[str]:
    """Why a command may not be auto-allowed (None when it may): it spans several lines, has a comment, a heredoc /
    here-string, an ANSI-C $'...' string or a command substitution, or a token holds a line break. The analysis of
    such text can miss a command, so an "allow" becomes "ask" (never a denial on its own)."""
    text = command_line.rstrip()
    if "\n" in text or "\r" in text:
        return "it spans several lines"
    for marker, label in AUTO_ALLOW_BLOCKERS:
        if marker in text:
            return f"it contains {label}"
    if _scan_shell(text)["has_comment"]:
        return "it contains a # comment"
    if any("\n" in t or "\r" in t or QUOTED_NEWLINE_SENTINEL in t for t in _tokenize(text)):
        return "an argument holds a line break"
    return None


def _line_tokens(text: str) -> List[str]:
    """Fallback tokens: each line on its own, and the lines that still do not parse split on whitespace / operators
    with their quote characters stripped (never a denial on its own; nothing is hidden)."""
    out: List[str] = []
    for n, line in enumerate(text.split("\n")):
        if n:
            out.append("\n")
        try:
            out += _shlex_tokens(line)
        except Exception:
            out += [t.strip("'\"") for t in re.split(r"\s+|(?=[;&|<>])|(?<=[;&|<>])", line)]
    return out


def _code_tokens(code: str, open_at: Optional[int]) -> List[str]:
    """shlex tokens of a code piece of _scan_shell. With an unterminated quote opening on the line at open_at, the
    complete lines before it are tokenized together (their quotes are balanced) and the rest line by line, so the
    open quote cannot swallow an earlier line (bash runs those before failing)."""
    try:
        return _shlex_tokens(code)
    except Exception:
        if not open_at:
            return _line_tokens(code)
    try:
        head = _shlex_tokens(code[:open_at])
    except Exception:
        head = _line_tokens(code[:open_at])
    return head + _line_tokens(code[open_at:])


def _heredoc_body_tokens(heredoc: Dict[str, Any], drop_interpreter_bodies: bool, depth: int) -> List[str]:
    """Tokens of a heredoc body, isolated from the surrounding text (a quote in the body never pairs with one
    outside it) and framed by line breaks. A body fed to python / node is dropped when drop_interpreter_bodies
    (the ground-truth line check: its code is judged by the inline write markers on the raw text). Any other body
    is judged as shell lines: scanned like a script (comments, quotes and nested heredocs as a shell reading it
    would see them); when that scan removed a comment or let a quote span lines, every body line is also judged on
    its own (a body that is data or another language: # is not a comment there and an apostrophe must not hide the
    lines after it). When the scan fails (ShellScanError: a case command inside $(...), nesting) the denial
    propagates for a body a shell runs (_heredoc_feeds_shell); any other body falls back to the line-by-line
    tokens."""
    if heredoc["interpreter"] and drop_interpreter_bodies:
        return ["\n"]
    body = heredoc["body"]
    try:
        tokens, scan = _tokenize_scan(body, drop_interpreter_bodies, depth + 1)
    except ShellScanError:
        if heredoc.get("shell", True):
            raise  # a shell runs this body: fail closed
        # data for another program (cat > f.sh, tee, perl ...): judged line by line instead, still strict
        return ["\n"] + _line_tokens(body) + ["\n"]
    out = ["\n"] + tokens
    if scan["has_comment"] or scan["open_at"] is not None or any(QUOTED_NEWLINE_SENTINEL in t for t in tokens):
        out += ["\n"] + _line_tokens(body)
    return out + ["\n"]


def _tokenize_scan(command_line: str, drop_interpreter_bodies: bool = False,
                   depth: int = 0) -> Tuple[List[str], Dict[str, Any]]:
    if depth > HEREDOC_NESTING_LIMIT:
        raise ShellScanError(f"heredocs nested deeper than {HEREDOC_NESTING_LIMIT} levels")
    scan = _scan_shell(command_line)
    out: List[str] = []
    last = len(scan["pieces"]) - 1
    for k, (kind, value) in enumerate(scan["pieces"]):
        if kind == "code":
            out += _code_tokens(value, scan["open_at"] if k == last else None)
        else:
            out += _heredoc_body_tokens(value, drop_interpreter_bodies, depth)
    for heredoc in scan["deferred"]:
        out += _heredoc_body_tokens(heredoc, drop_interpreter_bodies, depth)
    return out, scan


def _tokenize(command_line: str, drop_interpreter_bodies: bool = False) -> List[str]:
    """Quote-aware tokens (shell operators split off) of the text as bash reads it (_scan_shell): comments and line
    continuations removed, each heredoc body tokenized on its own right after its operator line
    (_heredoc_body_tokens), an unterminated quote confined to the lines from where it opens (_code_tokens). Line
    breaks inside quotes are still QUOTED_NEWLINE_SENTINEL here, so a quoted line break ('<LF>') is never taken for
    a "\\n" command separator: split on SHELL_SEPARATORS first, then pass every kept token through
    _restore_quoted_newline. A scanner failure (ShellScanError) propagates: the hook denies."""
    return _tokenize_scan(command_line, drop_interpreter_bodies)[0]


def _restore_quoted_newline(tok: str) -> str:
    """Turns QUOTED_NEWLINE_SENTINEL back into the line break it stands for, as bash would pass it: nested command
    strings (bash -c, eval, here-strings) are judged with their real lines and a lone quoted line break
    (eval '<LF>' rm -rf logs) is an argument holding a real newline, never the sentinel."""
    return tok.replace(QUOTED_NEWLINE_SENTINEL, "\n")


def _unwrap_subcommand(tokens: List[str], depth: int = 0) -> List[List[str]]:
    """Unwraps nested execution wrappers (wsl.exe ... --, sh/bash/zsh/dash -c / -lc '...') down to the
    underlying sub-commands so that flags and programs are visible to downstream checks. Beyond
    NESTED_DEPTH_LIMIT levels, fails closed by returning the tokens unwrapped."""
    if depth > NESTED_DEPTH_LIMIT:
        return [tokens]
    if not tokens:
        return [tokens]
    idx = _program_index(tokens)
    if idx >= len(tokens):
        return [tokens]
    prog = os.path.basename(tokens[idx]).lower()
    if prog.endswith(".exe"):
        prog = prog[:-4]
    if prog == "wsl":
        linux_cmd = _wsl_command(tokens[idx + 1:])
        if linux_cmd:
            return _unwrap_subcommand(linux_cmd, depth + 1)
        return [tokens]
    if prog in SHELL_INTERPRETERS:
        parsed = _shell_args(tokens[idx + 1:])
        inner = parsed.get("command")
        if inner is not None:
            subs = split_subcommands(inner)
            unwrapped: List[List[str]] = []
            for s in subs:
                unwrapped.extend(_unwrap_subcommand(s, depth + 1))
            return unwrapped if unwrapped else [tokens]
        return [tokens]
    return [tokens]


def split_subcommands(command_line: str) -> List[List[str]]:
    """Quote-aware split of compound shell commands (&&, ||, ;, |, &, newlines). A quoted line break stays inside
    its sub-command as an argument holding a real newline."""
    subcommands: List[List[str]] = []
    current: List[str] = []
    for tok in _tokenize(command_line):
        if not tok:
            continue
        if tok in SHELL_SEPARATORS:
            if current:
                subcommands.append(current)
                current = []
        else:
            current.append(_restore_quoted_newline(tok))
    if current:
        subcommands.append(current)
    return subcommands


def _tokenize_subcommand(cmd: str) -> List[str]:
    subs = split_subcommands(cmd)
    return [t for s in subs for t in s]


def _resolve_long(name: str, options: Dict[str, Any]) -> Optional[str]:
    """getopt_long / git parse-options matching: the exact option name or its unique prefix; None when unknown or
    ambiguous (the program would then reject it)."""
    if name in options:
        return name
    candidates = [o for o in options if o.startswith(name)] if name else []
    return candidates[0] if len(candidates) == 1 else None


def _wrapper_options(name: str, tokens: List[str], i: int, info: Dict[str, Any]) -> int:
    """Walks the options (and positional operands) of wrapper `name` from tokens[i]; returns the index after them.
    Records env -C / sudo -D directories, time -o files, env -S / flock -c command strings."""
    short_values, long_options, positionals = WRAPPER_SPECS[name]
    n = len(tokens)
    while i < n:
        a = tokens[i]
        if a == "--":
            i += 1
            break
        if a == "-" and name == "env":  # env - = env -i
            i += 1
            continue
        if not a.startswith("-") or a == "-":
            break
        i += 1
        if a.startswith("--"):
            opt, eq, value = a[2:].partition("=")
            key = _resolve_long(opt, long_options) or opt
            if long_options.get(key) is True and not eq and i < n:
                value, i = tokens[i], i + 1
        else:
            key, value = "", None
            for k, ch in enumerate(a[1:]):
                if ch in short_values:
                    key, value = ch, a[k + 2:]
                    if not value and i < n:
                        value, i = tokens[i], i + 1
                    break
        if key in ("C", "chdir") and name == "env" or key in ("D", "chdir") and name == "sudo":
            info["chdirs"].append(value or "")
        elif key in ("R", "chroot") and name == "sudo":
            info["chdirs"].append(UNKNOWN_VALUE)  # paths now resolve inside another root
        elif key in ("o", "output") and name == "time":
            info["outputs"].append(value or "")
        elif key in ("c", "command") and name == "flock":
            info["nested"].append(value or "")
        elif key in ("S", "split-string") and name == "env":
            # env -S 'X=1 cmd args' [more args]: the string is split into the command line it runs
            info["nested"].append(" ".join([value or ""] + [shlex.quote(t) for t in tokens[i:]]))
            return n
    i += min(positionals, n - i)
    if name == "flock" and i < n and (tokens[i] in ("-c", "--command") or tokens[i].startswith("--command=")):
        a = tokens[i]  # flock FILE -c 'cmd'
        info["nested"].append(a.split("=", 1)[1] if "=" in a else (tokens[i + 1] if i + 1 < n else ""))
        return n
    return i


def _xargs_options(tokens: List[str], i: int, info: Dict[str, Any]) -> int:
    """Walks xargs options from tokens[i] (GNU findutils: -I R, -i[R], -n N, -P N, -d C, -0, -a FILE, --replace=R
    ...); records them in info["xargs"] = {arg_file, delimiter, replace, cmd_index}; returns the command index."""
    x: Dict[str, Any] = {"arg_file": False, "delimiter": False, "replace": None, "cmd_index": i}
    n = len(tokens)
    while i < n:
        a = tokens[i]
        if a == "--":
            i += 1
            break
        if not a.startswith("-") or a == "-":
            break
        i += 1
        if a.startswith("--"):
            opt, eq, value = a[2:].partition("=")
            key = _resolve_long(opt, XARGS_LONG_OPTIONS)
            if XARGS_LONG_OPTIONS.get(key) is True and not eq and i < n:
                value, i = tokens[i], i + 1
            if key == "arg-file":
                x["arg_file"] = True
            elif key == "delimiter":
                x["delimiter"] = True
            elif key == "replace":
                x["replace"] = value if eq else "{}"
            continue
        letters = a[1:]
        for k, ch in enumerate(letters):
            if ch in XARGS_VALUE_SHORT:
                value = letters[k + 1:]
                if not value and i < n:
                    value, i = tokens[i], i + 1
                if ch == "a":
                    x["arg_file"] = True
                elif ch == "d":
                    x["delimiter"] = True
                elif ch == "I":
                    x["replace"] = value
                break
            if ch in XARGS_OPTIONAL_SHORT:
                if ch == "i":
                    x["replace"] = letters[k + 1:] or "{}"
                break
    x["cmd_index"] = i
    info["xargs"] = x
    return i


def _command_start(tokens: List[str]) -> Tuple[int, Dict[str, Any]]:
    """Cached _command_start_uncached (a command line re-judges the same sub-commands for every possible cwd and
    nesting level). Callers must not mutate the returned info."""
    return _command_start_cached(tuple(tokens))


@functools.lru_cache(maxsize=4096)
def _command_start_cached(tokens: Tuple[str, ...]) -> Tuple[int, Dict[str, Any]]:
    return _command_start_uncached(list(tokens))


def _command_start_uncached(tokens: List[str]) -> Tuple[int, Dict[str, Any]]:
    """(index of the executed program, prefix info). Skips VAR=value / VAR+=value assignments, shell keywords
    (if / then / do / { / !), `uv run` and wrappers with their options and operands (env -u X -C dir -S str,
    sudo -u user -D dir, timeout -s SIG 5, nice -n 10, stdbuf -oL, time -o file, flock file, xargs -I {} -n 1 ...).
    info = {assigns: [(var, value)] (VAR+=v is recorded as ${VAR}v), chdirs, outputs, nested, wrappers,
    xargs: {arg_file, delimiter, replace, cmd_index} | None}."""
    info: Dict[str, Any] = {"assigns": [], "chdirs": [], "outputs": [], "nested": [], "wrappers": [], "xargs": None}
    i, n = 0, len(tokens)
    while i < n:
        tok = tokens[i]
        m = ENV_ASSIGN_RE.match(tok)
        if m:
            info["assigns"].append((m.group(1), ("${" + m.group(1) + "}" if m.group(2) else "") + m.group(3)))
            i += 1
            continue
        if tok in SHELL_PREFIX_KEYWORDS:
            i += 1
            continue
        base = os.path.basename(tok).lower()
        if base == "uv" and i + 1 < n and tokens[i + 1] == "run":
            info["wrappers"].append("uv")
            i += 2
        elif base == "xargs":
            info["wrappers"].append("xargs")
            i = _xargs_options(tokens, i + 1, info)
        elif base in WRAPPER_SPECS:
            info["wrappers"].append(base)
            i = _wrapper_options(base, tokens, i + 1, info)
        else:
            return i, info
    return n, info


def _program_index(tokens: List[str]) -> int:
    """Index of the executed program, skipping VAR=value assignments, shell keywords and wrappers (env, nohup,
    timeout, sudo, xargs ... with their options)."""
    return _command_start(tokens)[0]


def _program(tokens: List[str]) -> str:
    idx = _program_index(tokens)
    return os.path.basename(tokens[idx]).lower() if idx < len(tokens) else ""


def _flags(tokens: List[str]) -> set:
    return {tok.split("=", 1)[0].lower() for tok in tokens if tok.startswith("-")}


def executor_confirmed(cmd: str, tokens: Optional[List[str]] = None, depth: int = 0) -> bool:
    """
    True only if the executor itself would receive --confirmed / --user-confirmed (exact tokens; abbreviations and
    =value forms are not recognised, which fails closed). Flags are read from the tokens after the
    execute_futures_trade script path (not env assignments or wrappers before it) and up to a shell comment
    or redirect.
    When the script sits inside the -c / --command string of sh/bash/... (parsed by _shell_args, e.g.
    wsl.exe -- bash -lc '...', bash -eo pipefail -c '...'), that string is re-tokenised and every segment running
    the executor must be confirmed; arguments after it are the shell's $0/$1..., never the executor's.
    """
    if depth > NESTED_DEPTH_LIMIT:
        return False
    if tokens is None:
        matching_subs = [seg for seg in split_subcommands(cmd) if TRADE_ENGINE_RE.search(" ".join(seg))]
        if not matching_subs:
            return False
        return all(executor_confirmed("", seg, depth + 1) for seg in matching_subs)

    tokens = list(tokens)
    idx = _program_index(tokens)
    while idx < len(tokens) and not TRADE_ENGINE_RE.search(tokens[idx]):
        idx += 1
    if idx >= len(tokens):
        return False
    for p in range(_program_index(tokens), idx):
        prog = os.path.basename(tokens[p]).lower()
        if prog.endswith(".exe"):
            prog = prog[:-4]
        if prog not in SHELL_INTERPRETERS:
            continue
        inner = _shell_args(tokens[p + 1:])["command"]
        if inner is not None:  # executor flags live only inside the -c/--command string; later words are $0/$1...
            segments = [seg for seg in split_subcommands(inner) if TRADE_ENGINE_RE.search(" ".join(seg))]
            # The string decides on its own: no executor segment inside (e.g. bash -c '$0 $1 ...' script --confirmed)
            # means the executor's own arguments cannot be verified -> not confirmed (fails closed).
            return bool(segments) and all(executor_confirmed("", seg, depth + 1) for seg in segments)
    for tok in tokens[idx + 1:]:
        if tok.startswith("#") or _is_redirect(tok):
            break
        if tok in EXECUTOR_CONFIRM_OPTIONS:
            return True
    return False


def _symbol_count(text: str) -> int:
    return len({m.group(2).upper() for m in re.finditer(r"--symbol(?:\s+|=)(['\"]?)([A-Za-z0-9_]+)\1", text)})


def _executed_script_at(tokens: List[str], in_wsl: bool = False) -> Tuple[List[str], int, bool]:
    """(tokens, index of the script operand in them, run inside wsl) for the script a sub-command actually runs: the
    program itself (./scripts/x.py) or the first operand of a python interpreter (python3 -u scripts/x.py), also
    through wsl.exe [-d X] [--cd X] [-u X] [--|-e] cmd... (the tokens are then the Linux command). Index -1 for
    python -c / -m / stdin."""
    idx = _program_index(tokens)
    n = len(tokens)
    if idx >= n:
        return tokens, -1, in_wsl
    program = os.path.basename(tokens[idx]).lower()
    if re.sub(r"\.exe$", "", program) == "wsl":
        linux_command = _wsl_command(tokens[idx + 1:])
        return _executed_script_at(linux_command, True) if linux_command else (tokens, idx, in_wsl)
    if not PYTHON_PROGRAM_RE.match(program):
        return tokens, idx, in_wsl
    i = idx + 1
    while i < n:
        tok = tokens[i]
        if tok == "--":
            return tokens, (i + 1 if i + 1 < n else -1), in_wsl
        if tok == "-":
            return tokens, -1, in_wsl
        if not tok.startswith("-"):
            return tokens, i, in_wsl
        i += 1
        if tok.startswith("--"):
            if tok in PYTHON_LONG_VALUE_OPTIONS:
                i += 1
            continue
        letters = tok[1:]
        for k, ch in enumerate(letters):
            if ch in "cm":
                return tokens, -1, in_wsl
            if ch in "WX":
                if k == len(letters) - 1:
                    i += 1
                break
    return tokens, -1, in_wsl


def _executed_script(tokens: List[str]) -> str:
    """Script operand actually run by a sub-command (see _executed_script_at); "" for python -c / -m / stdin."""
    toks, i, _ = _executed_script_at(tokens)
    return toks[i] if i >= 0 else ""


def _lexical_host_path(path: str, git_bash: bool = True) -> str:
    """Absolute POSIX spelling of a path for the lexical identity check, "" for a relative one: '\\' -> '/';
    C:/x, /mnt/C/x and (git_bash, a Windows-side spelling) Git Bash /c/x -> /mnt/c/x, lower-cased as a whole like
    _canon_path (drive paths live on case-insensitive NTFS); (git_bash too) \\\\wsl.localhost\\<distro>\\x and
    \\\\wsl$\\<distro>\\x -> /x only when <distro> is this WSL distribution (WSL_DISTRO_NAME, case-insensitive;
    issue #110): another or an unknown distro keeps its //wsl.../<distro>/x spelling, which never equals a workspace path (case kept: a Linux
    filesystem may hold an agent-made Scripts/ next to scripts/); normalised. Pass git_bash=False inside wsl.exe and
    whenever the session cwd is not a Windows-side spelling (_windows_side_cwd): /c/x is then a Linux directory and
    //wsl.localhost/... a Linux path (POSIX // root), never mapped."""
    p = (path or "").replace("\\", "/")
    m = re.match(r"^//(?:wsl\.localhost|wsl\$)/([^/]+)(?=/|$)", p, re.IGNORECASE) if git_bash else None
    if m:
        own = os.environ.get("WSL_DISTRO_NAME", "")
        if own and m.group(1).lower() == own.lower():
            return posixpath.normpath("/" + p[m.end():].lstrip("/"))
        return posixpath.normpath(p)
    m = (re.match(r"^([A-Za-z]):(?=/|$)", p) or re.match(r"^/mnt/([A-Za-z])(?=/|$)", p)
         or (re.match(r"^/([A-Za-z])(?=/|$)", p) if git_bash else None))
    if m:
        return posixpath.normpath("/mnt/" + m.group(1).lower() + p[m.end():]).lower()
    return posixpath.normpath(p) if p.startswith("/") else ""


def _windows_side_cwd(cwd: str) -> bool:
    """True when the session cwd is a Windows-side spelling (C:\\x, c:/x, any backslash, Git Bash /c/x), i.e. the hook
    judges a call made from Windows, where a Git Bash /c/... path is the C: drive (issue #110). A native Linux cwd
    (/mnt/c/..., /home/...) or no cwd: /c/... is a Linux directory."""
    cwd = cwd or ""
    return bool(re.match(r"^[A-Za-z]:", cwd) or "\\" in cwd or re.match(r"^/[A-Za-z](?=/|$)", cwd))


def _sanctioned_script(script: str, cwd: str, base_dir: str, in_wsl: bool,
                       windows_cwd: Optional[bool] = None, keys=None, allow_worktree: bool = True) -> Optional[str]:
    """RISK_REDUCING_SCRIPTS key (or one of keys) of a script operand, compared lexically (no filesystem check) with
    the sanctioned repo paths under base_dir (and, when allow_worktree, of a linked worktree). A relative operand is
    joined with the cwd (else base_dir). Inside wsl.exe the Linux
    path must be relative or absolute POSIX (/mnt/<drive>/... mapping to base_dir, or base_dir itself when the hook
    runs inside WSL); a Windows spelling or a backslash there names another file for Linux: None. windows_cwd (default
    _windows_side_cwd(cwd)): Git Bash /c/x spellings (script or cwd) are the C: drive only for a Windows-side cwd."""
    keys = RISK_REDUCING_SCRIPTS if keys is None else keys
    full = _script_host_path(script, cwd, base_dir, in_wsl, windows_cwd)
    if not full:
        return None
    root = _lexical_host_path(base_dir)
    for key in keys:
        if full == _lexical_host_path(posixpath.join(root, key), git_bash=False):
            return key
    wt_rel = _linked_worktree_rel(full, base_dir) if allow_worktree else ""
    if wt_rel in keys:
        return wt_rel
    return None


def _script_host_path(script: str, cwd: str, base_dir: str, in_wsl: bool,
                      windows_cwd: Optional[bool] = None) -> str:
    """Lexical host path (_lexical_host_path) of a path operand for _sanctioned_script: "" when it cannot name a file
    under base_dir (a Windows spelling inside wsl.exe, no base_dir)."""
    if not script or not base_dir:
        return ""
    if in_wsl and ("\\" in script or re.match(r"^[A-Za-z]:", script)):
        return ""
    if windows_cwd is None:
        windows_cwd = _windows_side_cwd(cwd)
    root = _lexical_host_path(base_dir)
    if not root:
        return ""
    if script.replace("\\", "/").startswith("/") or re.match(r"^[A-Za-z]:", script):
        return _lexical_host_path(script, git_bash=windows_cwd and not in_wsl)
    # The cwd is the session cwd (wsl.exe inherits it): Git Bash /c/x spellings map only from Windows
    start = (_lexical_host_path(cwd, git_bash=windows_cwd) if cwd else "") or root
    return _lexical_host_path(posixpath.join(start, script.replace("\\", "/")), git_bash=False)


def _risk_flags_allowed(key: str, args: List[str]) -> bool:
    """True when the arguments of a sanctioned script use only its allowlisted flags (RISK_REDUCING_SCRIPTS), carry
    one of its required flags (when it has any) and no positional operand; a value-taking flag needs a value that is
    not itself an option."""
    required, allowed = RISK_REDUCING_SCRIPTS[key]
    seen = set()
    i = 0
    while i < len(args):
        name, eq, _ = args[i].partition("=")
        if not name.startswith("-") or name in ("-", "--") or name not in allowed:
            return False
        if allowed[name]:
            if not eq:
                if i + 1 >= len(args) or args[i + 1].startswith("-"):
                    return False
                i += 1
        elif eq:
            return False  # argparse rejects a value on a switch
        seen.add(name)
        i += 1
    if required and not seen & required:
        return False
    if key == "scripts/execute_futures_trade.py" and seen & EXECUTOR_MOVE_BREAKEVEN_FLAGS and \
            sum(1 for a in args if a.partition("=")[0] == "--symbol") != 1:
        return False  # --move-breakeven acts on exactly one --symbol
    if seen & set(EXECUTOR_BREAKEVEN_YOLO_FLAGS) and not seen & EXECUTOR_MOVE_BREAKEVEN_FLAGS:
        return False  # --is-yolo outside --move-breakeven shapes an opening
    return True


def _is_breakeven_yolo_spelling(token: str) -> bool:
    """True for --is-yolo / --is_yolo or an argparse unique-prefix abbreviation of them (--is-y), switch form only."""
    name, eq, _ = token.partition("=")
    return not eq and len(name) > 3 and any(f.startswith(name) for f in EXECUTOR_BREAKEVEN_YOLO_FLAGS)


def _executor_opening_named(text: str) -> bool:
    """True when the text names an executor opening option (EXECUTOR_OPENING_OPTIONS, also as an argparse unique
    prefix such as --dir / --lev) anywhere, also inside a nested -c string."""
    for word in re.findall(r"--[A-Za-z0-9_-]+", text or ""):
        if len(word) > 3 and any(opt.startswith(word) for opt in EXECUTOR_OPENING_OPTIONS):
            return True
    return False


def _subcommand_is_risk_reducing(tokens: List[str], text: str, cwd: str = "", base_dir: str = "") -> bool:
    """True when a sub-command runs one of RISK_REDUCING_SCRIPTS by its exact repo path (the script it executes,
    resolved against cwd, else base_dir, which defaults to the workspace root) with only that script's allowlisted
    flags. Batch deploy scripts are never risk-reducing."""
    if not tokens or DEPLOY_BATCH_RE.search(text):
        return False
    toks, i, in_wsl = _executed_script_at(tokens)
    if i < 0:
        return False
    key = _sanctioned_script(toks[i], cwd, base_dir or find_workspace_root(), in_wsl, _windows_side_cwd(cwd))
    return bool(key) and _risk_flags_allowed(key, _plain_args(toks[i + 1:]))


def _read_only_script_key(tokens: List[str], cwd: str = "", base_dir: str = "") -> Optional[str]:
    """READ_ONLY_SCRIPTS key of the script a sub-command runs, by its exact repo path under base_dir (never a linked
    worktree copy: unreviewed code), else None."""
    if not tokens:
        return None
    toks, i, in_wsl = _executed_script_at(tokens)
    if i < 0:
        return None
    return _sanctioned_script(toks[i], cwd, base_dir or find_workspace_root(), in_wsl, _windows_side_cwd(cwd),
                              keys=READ_ONLY_SCRIPTS, allow_worktree=False)


def _read_only_path_blocker(key: str, flag: str, value: str, cwd: str, base_dir: str, in_wsl: bool) -> Optional[str]:
    """Why a path flag value of a read-only analysis script blocks the auto-allow: it must resolve inside logs/ (the
    lexical path and the os.path.realpath of base_dir/<that path>, so a symlink cannot lead out) and a write flag
    must name one of the script's own outputs (never another writer's file: GROUND_TRUTH_FILES,
    READ_ONLY_FOREIGN_OUTPUTS, logs/evaluations/). None when it may."""
    _allowed, path_flags, own = READ_ONLY_SCRIPTS[key]
    if not value or SHELL_GLOB_RE.search(value) or "~" in value:
        return f"{flag} {value!r} is not a plain path"
    full = _script_host_path(value, cwd, base_dir, in_wsl)
    root = _lexical_host_path(base_dir)
    if not full or not root or not full.startswith(root.rstrip("/") + "/logs/"):
        return f"{flag} {value!r} does not resolve inside logs/"
    rel = full[len(root.rstrip("/")) + 1:]
    real_logs = os.path.realpath(os.path.join(base_dir, "logs"))
    real = os.path.realpath(os.path.join(base_dir, *rel.split("/")))
    if os.path.commonpath([real_logs, real]) != real_logs or real == real_logs:
        return f"{flag} {value!r} does not resolve inside logs/"
    if path_flags[flag] != "write":
        return None
    real_rel = "logs/" + os.path.relpath(real, real_logs).replace(os.sep, "/")
    foreign = (set(GROUND_TRUTH_FILES) | READ_ONLY_FOREIGN_OUTPUTS) - set(own)
    for candidate in (rel, real_rel):
        name = candidate.lower().rstrip(". ")
        if ":" in candidate or EVALUATION_TRAIL_TARGET_RE.search(name) or name in foreign:
            return f"{flag} {value!r} names a file another script writes"
        if candidate not in own:
            return f"{flag} {value!r} is not one of the script's own outputs ({', '.join(own)})"
    return None


def _read_only_blocker(key: str, tokens: List[str], cwd: str = "", base_dir: str = "") -> Optional[str]:
    """Why a read-only analysis sub-command (READ_ONLY_SCRIPTS key) may not be auto-allowed, None when it may: the
    risk-reducing blockers (_risk_auto_allow_blocker: shell metacharacters, redirects, wrappers, env assignments,
    interpreter options), a flag outside the script's allowlist (abbreviations included), a value on a switch, a
    missing value, a stray operand, or a path flag that fails _read_only_path_blocker."""
    blocker = _risk_auto_allow_blocker(tokens)
    if blocker:
        return blocker
    base_dir = base_dir or find_workspace_root()
    toks, i, in_wsl = _executed_script_at(tokens)
    allowed, path_flags, _own = READ_ONLY_SCRIPTS[key]
    args = _plain_args(toks[i + 1:])
    j = 0
    while j < len(args):
        name, eq, value = args[j].partition("=")
        if name not in allowed:
            return f"it passes {args[j]!r}, which is not one of the script's read-only flags"
        if allowed[name]:
            if not eq:
                if j + 1 >= len(args) or args[j + 1].startswith("-"):
                    return f"{name} has no value"
                j += 1
                value = args[j]
            if name in path_flags:
                why = _read_only_path_blocker(key, name, value, cwd, base_dir, in_wsl)
                if why:
                    return why
        elif eq:
            return f"{name} takes no value"
        j += 1
    return None


def _is_redirect(tok: str) -> bool:
    """Output redirect operator: >, >>, >|, &>, >&, <> and any other punctuation-only run containing '>'."""
    return tok in REDIRECT_TOKENS or (">" in (tok or "") and all(c in SHELL_PUNCTUATION for c in tok))


def _redirect_targets(tokens: List[str]) -> List[str]:
    """Files written by output redirects (> f, >> f, &> f, 1<> f); fd duplications (2>&1, >&-) are not files."""
    targets = []
    for i, tok in enumerate(tokens):
        if _is_redirect(tok) and i + 1 < len(tokens):
            if tok.endswith(">&") and (tokens[i + 1].isdigit() or tokens[i + 1] == "-"):
                continue
            targets.append(tokens[i + 1])
    return targets


def _is_inline_code(command_line: str) -> bool:
    return bool(
        INLINE_PYTHON_RE.search(command_line)
        or STDIN_PYTHON_RE.search(command_line)
        or "<<" in command_line
        or PIPE_TO_INTERPRETER_RE.search(command_line)
        or OTHER_INLINE_RE.search(command_line)
    )


def _subcommand_writes_path(tokens: List[str], text: str, path_re: re.Pattern, inline: bool) -> bool:
    """True when a sub-command can modify a path matched by path_re."""
    if any(path_re.search(t) for t in _redirect_targets(tokens)):
        return True
    prog = _program(tokens)
    if prog in WRITE_PROGRAMS and path_re.search(text):
        return True
    if prog in ("sed", "perl") and any(t.startswith("-i") or t == "--in-place" for t in tokens) and path_re.search(text):
        return True
    if prog == "git" and re.search(r"\bgit\s+(?:checkout|restore|rm|mv|apply|reset)\b", text) and path_re.search(text):
        return True
    if inline and path_re.search(text) and INLINE_WRITE_MARKERS_RE.search(text):
        return True
    return False


# -----------------------------------------------------------------------------
# Ground-truth runtime state (session_state / guardian_state / pending_entries / hook_heartbeat)
# -----------------------------------------------------------------------------
def ground_truth_denial(paths: List[str]) -> str:
    """Denial reason naming each protected file and its sole sanctioned writer (GROUND_TRUTH_FILES order), and the
    git config / hook channel when GIT_EXEC_CONFIG_KEY is among the hits."""
    keys = [p for p in GROUND_TRUTH_FILES if p in set(paths)]
    git_channel = GIT_EXEC_CONFIG_KEY in set(paths)
    if git_channel and not keys:
        return GIT_EXEC_CONFIG_REASON
    reason = "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Ground Truth Protection): " + "; ".join(
        f"{p} may only be written by {GROUND_TRUTH_FILES[p]}" for p in keys
    ) + "."
    return reason + " " + GIT_EXEC_CONFIG_REASON if git_channel else reason


def _protected_hits(hits: List[str]) -> List[str]:
    """Ground-truth keys (GROUND_TRUTH_FILES order) among the hits, then GIT_EXEC_CONFIG_KEY when present."""
    found = set(hits)
    return [p for p in GROUND_TRUTH_FILES if p in found] + ([GIT_EXEC_CONFIG_KEY] if GIT_EXEC_CONFIG_KEY in found
                                                            else [])


def _git_exec_config_path(word: str) -> bool:
    """A word (option value / braces expanded) naming a git config or hook file (GIT_EXEC_CONFIG_PATH_RE) or a
    directory holding them. A glob counts when a dot-component can expand to .git / .gitconfig* (.gi?/config,
    .git/hoo*/x) or it spells .config/git/<config glob>; shells never expand a bare * to a dot name."""
    for expanded in _expand_braces(_word_value(word or "")):
        p = _shell_path(expanded).lower()
        if not p:
            continue
        if GIT_EXEC_CONFIG_PATH_RE.search(p):
            return True
        if SHELL_GLOB_RE.search(p):
            parts = p.split("/")
            if any(c.startswith(".") and (fnmatch.fnmatchcase(".git", c) or fnmatch.fnmatchcase(".gitconfig", c))
                   for c in parts):
                return True
            if (len(parts) >= 3 and fnmatch.fnmatchcase(".config", parts[-3]) and fnmatch.fnmatchcase("git", parts[-2])
                    and fnmatch.fnmatchcase("config", parts[-1])):
                return True
    return False


def _ground_truth_named(text: str) -> List[str]:
    return [GROUND_TRUTH_BASENAMES[m.group(0).lower()] for m in GROUND_TRUTH_RE.finditer(text or "")]


def _shell_path(word: str) -> str:
    """Forward slashes, no Windows drive prefix, '.', '..' and trailing slashes collapsed."""
    p = re.sub(r"^[A-Za-z]:(?=/|$)", "", (word or "").replace("\\", "/"))
    return posixpath.normpath(p) if p else ""


def _expand_braces(word: str, limit: int = 64) -> List[str]:
    """Minimal bash brace expansion ({a,b}) so logs/{guardian_state,x}.json is seen as two words."""
    m = re.search(r"\{([^{}]*,[^{}]*)\}", word)
    if not m:
        return [word]
    out: List[str] = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(word[:m.start()] + alt + word[m.end():], limit))
        if len(out) >= limit:
            break
    return out[:limit]


def _glob_ground_truth(word: str) -> List[str]:
    """Protected files a glob / brace word inside a logs/ directory can expand to (logs/*.json, logs/*state*)."""
    if not SHELL_GLOB_RE.search(word or ""):
        return []
    hits: List[str] = []
    for expanded in _expand_braces(word):
        head, _, name = _shell_path(expanded).rpartition("/")
        if not head or not fnmatch.fnmatchcase("logs", head.rpartition("/")[2].lower()):
            continue
        hits.extend(key for base, key in GROUND_TRUTH_BASENAMES.items() if fnmatch.fnmatchcase(base, name.lower()))
    return hits


def _windows_form(word: str) -> bool:
    """A path spelled in Windows form (C:\\x, C:/x, \\\\server\\share) or a drive mount (/mnt/c/x, Git Bash /c/x)."""
    return bool(WINDOWS_FORM_PATH_RE.match((word or "").replace("\\", "/")))


def _is_logs_dir(word: str, cwd: str = "", base_dir: str = "") -> bool:
    """True when a word (braces expanded) names a `logs` directory: ANY path whose last component is `logs`
    (logs, ./logs/, ./x/../logs, /abs/repo/logs, /tmp/other/logs, build/logs, C:\\...\\logs, /mnt/c/.../logs,
    /proc/self/cwd/logs): symlinks (/tmp/r -> repo), /proc/<pid>/cwd, junctions and 8.3 names alias the workspace
    logs/ in ways the hook cannot resolve, so every directory named logs counts. A glob that can expand to logs
    (log*, lo[g]s, *) is resolved against the tracked cwd and the workspace root; when it cannot be resolved (no
    base_dir, a leading ~, a run-time value) it fails safe (True)."""
    logs = _canon_path(base_dir).rstrip("/") + "/logs" if base_dir else ""
    for expanded in _expand_braces(word or ""):
        sp = _shell_path(expanded)
        head, _, last = sp.rpartition("/")
        last = last.lower()
        if last == "logs":
            return True
        if not (SHELL_GLOB_RE.search(last) and fnmatch.fnmatchcase("logs", last)):
            continue
        if not base_dir or sp.startswith("~") or _unresolved(sp):
            return True  # cannot resolve the glob ($X/lo*, ~/lo*): fail safe
        if fnmatch.fnmatchcase(logs, _canon_path(sp, cwd or base_dir)):
            return True
    return False


# /proc/<pid>/cwd, /proc/<pid>/root, /proc/<pid>/fd (also via task/<tid>) and /dev/fd resolve against the process
# that runs the command, not the hook: a path through them is unresolvable (an unknown cwd / ancestor of logs/).
PROC_ALIAS_RE = re.compile(r"^(?:/proc/[^/]+/(?:task/[^/]+/)?(?:cwd|root|fd)|/dev/fd)(?:/|$)")


def _proc_alias(word: str) -> bool:
    """A path through /proc/<pid>/{cwd,root,fd} or /dev/fd (raw with repeated slashes / ./ squeezed, or normalised)."""
    raw = re.sub(r"/(?:\./)+", "/", re.sub(r"/+", "/", (word or "").replace("\\", "/")))
    return any(PROC_ALIAS_RE.match(p) for p in (raw, _shell_path(word or "")))


def _word_value(word: str) -> str:
    """Value of option/assignment words (of=..., --output=...), otherwise the word itself."""
    m = re.match(r"^-*[A-Za-z_][A-Za-z0-9_-]*=(.*)$", word)
    return m.group(1) if m else word


def _canon_path(path: str, cwd: str = "") -> str:
    """Lower-case absolute POSIX path without the Windows drive / WSL /mnt/<d> / Git Bash /<d> prefix."""
    p = (path or "").replace("\\", "/")
    m = re.match(r"^(?:/mnt/[A-Za-z]|/[A-Za-z]|[A-Za-z]:)(?=/|$)", p)
    if m:
        p = p[m.end():] or "/"
    if not p.startswith("/"):
        p = (_canon_path(cwd) if cwd else "") + "/" + p
    return "/" + posixpath.normpath(p).lstrip("/").lower()


def _reaches_logs_dir(word: str, cwd: str, base_dir: str) -> bool:
    """True when a word (braces/globs expanded) is the logs dir or one of its ancestors (., .., /, ~, the repo...),
    or a path through /proc/<pid>/cwd|root|fd or /dev/fd (unresolvable: counts as an ancestor)."""
    logs = _canon_path(base_dir).rstrip("/") + "/logs" if base_dir else "/logs"
    ancestors = [logs]
    while ancestors[-1] != "/":
        ancestors.append(posixpath.dirname(ancestors[-1]))
    for expanded in _expand_braces(word or ""):
        if not expanded:
            continue
        if _is_logs_dir(expanded, cwd, base_dir) or _proc_alias(expanded):
            return True
        sp = _shell_path(expanded)
        if sp in (".", "..", "/", "~") or sp.endswith("/.."):
            return True
        if sp.startswith("~/"):
            # Home is unknown to the hook: ~/Documents reaches the repo when that segment is one of its ancestors
            pattern = "*/" + sp[2:].lower()
        else:
            pattern = _canon_path(sp, cwd or base_dir)
        if any(fnmatch.fnmatchcase(a, pattern) for a in ancestors):
            return True
    return False


def _plain_args(args: List[str]) -> List[str]:
    """Arguments without redirect operators, their targets and the fd number glued before them (2>&1)."""
    out: List[str] = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if _is_redirect(a) or a in ("<", "<<", "<<<", "<&"):
            skip = True
            continue
        if a.isdigit() and i + 1 < len(args) and (_is_redirect(args[i + 1]) or args[i + 1] in ("<", "<&")):
            continue
        out.append(a)
    return out


def _split_operands(prog: str, args: List[str]) -> Tuple[List[str], Optional[str]]:
    """(operands, target directory) for coreutils-style args; -t DIR, -tDIR, -rtDIR, --target-directory[=]DIR."""
    operands: List[str] = []
    target: Optional[str] = None
    skip = end_of_options = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if end_of_options or not a.startswith("-") or a == "-":
            operands.append(a)
            continue
        if a == "--":
            end_of_options = True
            continue
        if prog not in TARGET_DIR_PROGRAMS:
            continue
        m = re.match(r"^--(t[\w-]*)(?:=(.*))?$", a, re.DOTALL)
        if m and "target-directory".startswith(m.group(1)):
            if m.group(2) is not None:
                target = m.group(2)
            elif i + 1 < len(args):
                target, skip = args[i + 1], True
            continue
        if a.startswith("--"):
            continue
        letters = a[1:]
        for j, ch in enumerate(letters):
            if ch == "t":
                if letters[j + 1:]:
                    target = letters[j + 1:]
                elif i + 1 < len(args):
                    target, skip = args[i + 1], True
                break
            if ch in "Smog":  # options taking a value (-S suffix, install -m/-o/-g)
                skip = not letters[j + 1:]
                break
    return operands, target


def _is_recursive(args: List[str]) -> bool:
    return any(re.match(r"^-[A-Za-z]*[rRa]", a) or a in ("--recursive", "--archive") for a in args)


def _exec_writes(args: List[str]) -> bool:
    """True when a find -exec/-ok command line (program + args) can modify files."""
    if not args:
        return False
    prog = os.path.basename(args[0]).lower()
    if prog in GROUND_TRUTH_READ_PROGRAMS:
        return False
    if prog in ("sed", "perl"):
        return any(re.match(r"^-[A-Za-z]*i", t) or t.startswith("--in-place") for t in args[1:])
    return True


def _find_roots(args: List[str]) -> List[str]:
    i = 0
    while i < len(args) and (args[i] in ("-H", "-L", "-P", "-D") or args[i].startswith("-O")):
        i += 2 if args[i] == "-D" else 1
    roots = []
    while i < len(args) and not args[i].startswith("-") and args[i] not in ("(", ")", "!", ","):
        roots.append(args[i])
        i += 1
    return roots or ["."]


def _find_ground_truth(args: List[str], cwd: str = "", base_dir: str = "", depth: int = 0) -> List[str]:
    """Protected files a `find` can write (-fprint ...) or delete (-delete, -exec rm, -exec sh ...)."""
    hits: List[str] = []
    for i, a in enumerate(args):
        if a in FIND_OUTPUT_ACTIONS and i + 1 < len(args):
            hits += _ground_truth_named(args[i + 1]) + _glob_ground_truth(args[i + 1])
    destructive = any(a in FIND_DELETE_ACTIONS for a in args)
    roots = _find_roots(args)
    unfiltered = any(a in ("-not", "!", "-o", "-or", ",") for a in args)
    filters = [(a.lower(), args[i + 1]) for i, a in enumerate(args)
               if a.lower() in FIND_NAME_FILTERS | FIND_PATH_FILTERS and i + 1 < len(args)]

    def can_match(name: str, path: str) -> bool:
        """Whether the (ANDed) -name/-path filters can match an entry with this name and path."""
        if unfiltered or not filters:
            return True
        return all(fnmatch.fnmatchcase((name if flt in FIND_NAME_FILTERS else path).lower(), pat.lower())
                   for flt, pat in filters)

    # {} expands to matched entries: a root the filters can match (-maxdepth 0 -name .) or the workspace logs/ dir
    # under a root that reaches it (-name 'lo*'); every -exec command is also judged with its literal operands
    # (find . -name x -exec rm -rf logs \;), before the name-filter shortcut below.
    matches = [r for r in roots if can_match(posixpath.basename(_shell_path(r)) or r, r)]
    matches += [posixpath.join(r, "logs") for r in roots
                if _reaches_logs_dir(r, cwd, base_dir) and not _is_logs_dir(r, cwd, base_dir)
                and can_match("logs", posixpath.join(r, "logs"))]
    exec_words: List[str] = []
    for i, a in enumerate(args):
        if a in FIND_EXEC_ACTIONS:
            end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args))
            command = args[i + 1:end]
            if _exec_writes(command):
                destructive = True
                exec_words += args[i + 2:end]
            for value in ["__find_match__"] + matches:
                sub = [t.replace("{}", value) for t in command]
                hits += _ground_truth_writes(sub, " ".join(sub), cwd, base_dir, depth + 1)
    if not destructive:
        return hits
    hits += _ground_truth_named(" ".join(args))
    if filters and not unfiltered:
        # Name/path filters are ANDed: only files they can match are reachable
        for flt, pattern in filters:
            pat = pattern.lower()
            for base, key in GROUND_TRUTH_BASENAMES.items():
                candidates = [base] if flt in FIND_NAME_FILTERS else (
                    [f"logs/{base}", f"./logs/{base}", f"/x/logs/{base}"]
                    + [posixpath.join(r, "logs", base).lower() for r in roots])
                if any(fnmatch.fnmatchcase(c, pat) for c in candidates):
                    hits.append(key)
        return hits
    # Negated / alternated filters or only -type, -mmin, -regex ...: every file under the roots is reachable,
    # and so is logs/ when the -exec command writes into it (find /tmp/f -type f -exec cp {} logs/ \;)
    if (any(_reaches_logs_dir(r, cwd, base_dir) for r in roots)
            or any(_is_logs_dir(w, cwd, base_dir) for w in exec_words)):
        hits.extend(GROUND_TRUTH_FILES)
    return hits


def _ground_truth_read_only(prog: str, args: List[str], assigned: bool = False,
                            read_programs: frozenset = frozenset(GROUND_TRUTH_READ_PROGRAMS)) -> bool:
    """True when a sub-command that names a protected file can only read it (read-only allowlist). Options that make
    an allowlisted program write a file or run a command (git --output / -O / -c, rg --pre, less -o) and leading
    VAR= assignments (LESSOPEN, GIT_EXTERNAL_DIFF, NODE_OPTIONS...) void the exemption. PowerShell commands pass
    read_programs extended with PS_READ_CMDLETS (Get-Content, Select-String...)."""
    if assigned:
        return False
    if prog in read_programs:
        if prog == "jq":
            return not any(a == "-i" or a.startswith("--in-place") for a in args)
        if prog == "rg":
            return not any(a in RG_EXEC_OPTIONS or a.startswith(tuple(o + "=" for o in RG_EXEC_OPTIONS))
                           for a in args)
        if prog == "less":
            return not any(a.startswith(("+", "--log-file", "--LOG-FILE")) or re.match(r"^-[^-]*[oO]", a)
                           for a in args)
        return True
    if prog == "find":
        return True  # judged by _find_ground_truth (destructive actions, -fprint outputs, -exec commands)
    if prog in ("for", "select", "case"):
        return True  # `for f in logs/*`: the word list is only expanded; the loop variable is a run-time value
    if prog == "git":
        # commit messages, greps and diffs may name them
        return _git_subcommand(args)[0] in GIT_READ_SUBCOMMANDS and not _git_runs_commands(args)
    if prog == "gh":
        return bool(args) and args[0] in ("pr", "issue")
    if prog.startswith("python"):
        if "-m" in args and "json.tool" in args:
            rest = args[args.index("json.tool") + 1:]
            positional = [a for i, a in enumerate(rest) if not a.startswith("-") and (i == 0 or rest[i - 1] != "--indent")]
            return len(positional) <= 1  # a second positional is the output file
        # python -c / python - (heredoc): inline code is judged by INLINE_WRITE_MARKERS_RE on the whole command
        for a in args:
            if a == "-" or re.match(r"^-[A-Za-z]*c$", a):
                return True
            if not a.startswith("-"):
                return False  # a script (python3 tool.py logs/...) is not read-only
        return False
    if prog in ("node", "nodejs"):
        return any(a in ("-e", "--eval", "-p", "--print") for a in args)
    return False


def _git_subcommand(args: List[str]) -> Tuple[str, List[str]]:
    """(sub-command, its args) after git's global options (git -C dir -c k=v clean -fdx -> 'clean', ['-fdx'])."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    return (args[i].lower(), args[i + 1:]) if i < len(args) else ("", [])


def _git_dir_option(args: List[str]) -> bool:
    """git's global --git-dir=<x> / --git-dir <x> (like GIT_DIR: git reads that repository's config and hooks)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "--git-dir" or args[i].startswith("--git-dir="):
            return True
        i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    return False


def _git_c_dir(args: List[str]) -> str:
    """The relative directory `git -C <dir>` changes the base to (cumulative), used to resolve operands against
    logs/ (git -C logs grep -O x -- '*.json' searches inside logs/)."""
    dirs: List[str] = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-C" and i + 1 < len(args):
            dirs.append(args[i + 1])
            i += 2
        else:
            i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    return "/".join(dirs)


def _git_work_trees(args: List[str]) -> List[str]:
    """Values of git's global --work-tree option (--work-tree=dir / --work-tree dir)."""
    out: List[str] = []
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a.startswith("--work-tree="):
            out.append(a.split("=", 1)[1])
        elif a == "--work-tree" and i + 1 < len(args):
            out.append(args[i + 1])
        i += 2 if a in GIT_GLOBAL_VALUE_OPTIONS else 1
    return out


def _git_base_dir_write(args: List[str], cwd: str, base_dir: str, strict: bool) -> bool:
    """A git sub-command that can write the work tree (anything outside GIT_READ_SUBCOMMANDS) run with `-C <dir>` or
    `--work-tree <dir>` inside logs/ (git -C logs checkout ., git --work-tree=logs checkout HEAD -- .) or, strict,
    an unresolved directory (git -C "$D" clean -fdx)."""
    sub, _ = _git_subcommand(args)
    if not sub or sub in GIT_READ_SUBCOMMANDS:
        return False
    cdir = _git_c_dir(args)
    for d in ([cdir] if cdir else []) + [_join_cwd(cdir, w) if cdir else w for w in _git_work_trees(args)]:
        if _unresolved(d):
            if strict:
                return True
            continue
        if _is_logs_dir(d, cwd, base_dir) or _cwd_in_logs(_join_cwd(cwd, d), base_dir):
            return True
    return False


def _pager_value_allowed(value: str) -> bool:
    """A pager command (GIT_PAGER / PAGER / MANPAGER, core.pager, pager.<cmd>) that cannot write or run anything:
    empty, or cat / less / more (bare or in /bin, /usr/bin) with read-only flags only (no +cmd, -o / -O / -k,
    --log-file)."""
    if not value.strip():
        return True
    try:
        words = shlex.split(value)
    except ValueError:
        return False
    prog = words[0]
    if not (prog in PAGER_PROGRAMS or any(prog == d + p for d in SYSTEM_BIN_DIRS for p in PAGER_PROGRAMS)):
        return False
    return _less_flags_allowed(words[1:])


def _less_flags_allowed(words: List[str]) -> bool:
    """Only flags, none of which runs a command or writes a file (+cmd, -o / -O log file, -k lesskey, --log-file)."""
    for w in words:
        if not w.startswith("-") or w.lower().startswith(("--log-file", "--lesskey")):
            return False
        if not w.startswith("--") and re.search(r"[oOk]", w[1:]):
            return False
    return True


def _editor_value_allowed(value: str) -> bool:
    """An editor command (EDITOR / VISUAL / GIT_EDITOR / GIT_SEQUENCE_EDITOR, core.editor, sequence.editor) that
    edits nothing: true, :, cat."""
    v = value.strip()
    return v in EDITOR_VALUES or any(v == d + e for d in SYSTEM_BIN_DIRS for e in ("true", "cat"))


def _literal(value: Optional[str]) -> bool:
    return value is not None and not _unresolved(value)


def _env_assignment_denied(var: str, value: Optional[str], prog: str, prog_token: str, wrappers: List[str]) -> bool:
    """Whether assigning env var `var` (value None = unknown) for program `prog` opens a command / config channel:
    injection vars and command channels (ENV_DENY_OUTRIGHT, GIT_CONFIG_KEY_*) always; ENV / BASH_ENV when the
    program is a shell, env, a script run by path, or nothing (standalone / export: it outlives the sub-command);
    pager / editor vars and LESS unless their value is an allowlisted literal."""
    if var in ENV_DENY_OUTRIGHT or var.startswith(ENV_DENY_PREFIXES):
        return True
    if var in ENV_STARTUP_VARS:
        return (not prog or prog in ASSIGNING_BUILTINS or prog in SHELL_INTERPRETERS or prog == "env"
                or "env" in wrappers or "/" in prog_token.replace("\\", "/"))
    if var in ENV_PAGER_VARS:
        return not (_literal(value) and _pager_value_allowed(value))
    if var in ENV_EDITOR_VARS:
        return not (_literal(value) and _editor_value_allowed(value))
    if var == "LESS":  # options less reads from the environment (LESS=FRX, LESS=-R)
        return not (_literal(value) and _less_flags_allowed(
            [w if w.startswith(("-", "+")) else "-" + w for w in value.split()]))
    return False


def _declared_assignments(prog: str, args: List[str]) -> List[Tuple[str, Optional[str]]]:
    """Variables a builtin assigns: export / declare / typeset / local / readonly NAME=value (NAME+=value as
    ${NAME}value; a bare NAME keeps its value: None; declare -n ref=NAME aliases NAME: unknown), and read NAME,
    printf -v NAME, mapfile NAME, getopts spec NAME, for / select NAME (values only known at run time)."""
    out: List[Tuple[str, Optional[str]]] = []
    if prog in DECLARE_BUILTINS:
        nameref = any(re.match(r"^-[A-Za-z]*n", a) for a in args)
        for a in args:
            m = ENV_ASSIGN_RE.match(a)
            if m:
                out.append((m.group(1), ("${" + m.group(1) + "}" if m.group(2) else "") + m.group(3)))
                if nameref:
                    out.append((m.group(3), UNKNOWN_VALUE))
            elif re.match(r"^[A-Za-z_]\w*$", a):
                out.append((a, None))
        return out
    names: List[str] = []
    if prog == "read":
        skip = False
        for j, a in enumerate(args):
            if skip:
                skip = False
            elif re.match(r"^-[A-Za-z]*[adinNptu]$", a):
                skip = True
                if a.endswith("a") and j + 1 < len(args):
                    names.append(args[j + 1])
            elif not a.startswith("-"):
                names.append(a)
        names = names or ["REPLY"]
    elif prog == "printf":
        names = [args[j + 1] for j, a in enumerate(args[:-1]) if a == "-v"]
    elif prog in ("mapfile", "readarray"):
        positional = [a for a in args if not a.startswith("-")]
        names = positional[-1:] or ["MAPFILE"]
    elif prog == "getopts":
        names = args[1:2]
    elif prog in ("for", "select"):
        names = args[:1]
    elif prog == "unset":
        names = [a for a in args if not a.startswith("-")]
        return [(n, "") for n in names]
    return [(n, UNKNOWN_VALUE) for n in names if re.match(r"^[A-Za-z_]\w*$", n)]


def _git_key_value_denied(key: str, value: Optional[str]) -> bool:
    """A git config key / value that runs a command or points git at code / config: dangerous keys are denied,
    except pager keys with an allowlisted pager (or a boolean for pager.<cmd>), editor keys with true / : / cat and
    protocol[.<name>].allow with never."""
    if not GIT_CONFIG_DANGEROUS_RE.match(key or ""):
        return False
    if GIT_CONFIG_PAGER_KEY_RE.match(key):
        return not (_literal(value) and (value.strip().lower() in GIT_BOOLEAN_VALUES or _pager_value_allowed(value)))
    if GIT_CONFIG_EDITOR_KEY_RE.match(key):
        return not (_literal(value) and _editor_value_allowed(value))
    if GIT_CONFIG_PROTOCOL_KEY_RE.match(key):
        return not (_literal(value) and value.strip().lower() == "never")
    return True


def _git_config_command_denied(rest: List[str]) -> bool:
    """`git config ...` (options with values parsed: -f/--file, --blob, -t/--type, --default, --comment, --value,
    --url; scopes; -z; --name-only; unique-prefix long options; get/set/unset/list/... sub-commands). A read only
    with an explicit read action (--get*, -l/--list, get, list); edit / rename-section are denied; otherwise a
    dangerous key (core.pager, alias.*, core.hooksPath ...) is denied unless its value is allowlisted."""
    actions: set = set()
    positionals: List[str] = []
    skip = False
    for j, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--":
            positionals += rest[j + 1:]
            break
        if a.startswith("--"):
            opt, eq, _ = a[2:].partition("=")
            key = _resolve_long(opt, GIT_CONFIG_LONG_OPTIONS)
            if key is None and opt.startswith("no-"):
                continue
            if key is None:
                actions.add("unknown")
                continue
            skip = GIT_CONFIG_LONG_OPTIONS[key] and not eq
            actions.add(key)
            continue
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch in GIT_CONFIG_VALUE_SHORT:
                    skip = not a[k + 2:]
                    break
                if ch == "l":
                    actions.add("list")
                elif ch == "e":
                    actions.add("edit")
            continue
        positionals.append(a)
    if positionals and positionals[0].lower() in GIT_CONFIG_SUBCOMMANDS:
        actions.add(positionals[0].lower())
        positionals = positionals[1:]
    if actions & GIT_CONFIG_READ_ACTIONS:
        return False
    if actions & {"edit", "rename-section"}:
        return True
    if not positionals:
        return False
    return _git_key_value_denied(positionals[0], positionals[1] if len(positionals) > 1 else None)


def _git_config_denied(args: List[str]) -> bool:
    """Git config that runs a command: `git -c key=value` / `--config-env=key=VAR` (hidden value: any dangerous
    key) with a dangerous key, `git config` persisting one (see _git_config_command_denied), `git clone -c key=value`
    and clone / init --template (hooks copied from a directory, like GIT_TEMPLATE_DIR)."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        a = args[i]
        if a == "-c" and i + 1 < len(args):
            key, eq, value = args[i + 1].partition("=")
            if _git_key_value_denied(key, value if eq else None):
                return True
        elif a.startswith("--config-env"):
            spec = a.split("=", 1)[1] if "=" in a else (args[i + 1] if i + 1 < len(args) else "")
            if GIT_CONFIG_DANGEROUS_RE.match(spec.split("=", 1)[0]):
                return True
        i += 2 if a in GIT_GLOBAL_VALUE_OPTIONS else 1
    sub, rest = _git_subcommand(args)
    if sub == "config":
        return _git_config_command_denied(rest)
    if sub in ("clone", "init"):
        for j, a in enumerate(rest):
            opt = a[2:].split("=", 1)[0] if a.startswith("--") else ""
            if opt and len(opt) >= 2 and "template".startswith(opt):
                return True
            if sub == "clone":
                if a in ("-c", "--config") and j + 1 < len(rest):
                    spec = rest[j + 1]
                elif a.startswith("--config="):
                    spec = a.split("=", 1)[1]
                elif re.match(r"^-c.", a):
                    spec = a[2:]
                else:
                    continue
                key, eq, value = spec.partition("=")
                if _git_key_value_denied(key, value if eq else None):
                    return True
    return False


def _git_long_options(arg: str) -> List[str]:
    """GIT_RUN_LONG_OPTIONS an argument can select: the exact name, a longer spelling (--output-directory) or a
    unique-prefix abbreviation parse-options accepts (--op='cmd', --upl=cmd, --rece=, --exe=)."""
    m = re.match(r"^--([A-Za-z][\w-]*)(?:=|$)", arg)
    if not m:
        return []
    name = m.group(1).lower()
    return [o for o, shortest in GIT_RUN_LONG_OPTIONS.items()
            if name.startswith(o) or (len(name) >= shortest and o.startswith(name))]


def _short_cluster_takes_next(letters: str, value_set: set) -> bool:
    """True when a short-option cluster's trailing option takes the *next* token as its value (its value is not
    glued): scanning left to right, stop at the first value-taking option; it consumes the next token only when it
    is the last character of the cluster (-m 1), not when a value is glued to it (-m1, -tjson)."""
    for i, c in enumerate(letters):
        if c in value_set:
            return not letters[i + 1:]
    return False


def _git_grep_cluster(arg: str) -> Dict[str, Any]:
    """Parse a git grep short cluster honouring value-taking options: -eOrder = -e Order (the pattern), NOT -O;
    -Orm / -nOrm = -O rm (open-files-in-pager). Returns {runs, pager, pattern, takes_next}."""
    out = {"runs": False, "pager": None, "pattern": False, "takes_next": False}
    if not re.match(r"^-[^-]", arg):
        return out
    letters = arg[1:]
    i = 0
    while i < len(letters):
        c = letters[i]
        if c == "O":                       # open-files-in-pager; the rest of the cluster is the pager (attached)
            out["runs"] = True
            if letters[i + 1:]:
                out["pager"] = letters[i + 1:]
            return out
        if c in GIT_GREP_VALUE_SHORT:      # value-taking: the rest of the cluster is its value, stop scanning
            if c in GIT_GREP_PATTERN_OPTS_SHORT:
                out["pattern"] = True
            out["takes_next"] = not letters[i + 1:]
            return out
        i += 1
    return out


def _git_short_cluster_runs(sub: str, arg: str) -> bool:
    """A short-option cluster that runs a command: grep -O<cmd> / -nOrm (but not -eOrder) or, for diff/log/show,
    an orderfile -o/-O that voids the read-only exemption."""
    if not re.match(r"^-[^-]", arg):
        return False
    if sub == "grep":
        return _git_grep_cluster(arg)["runs"]
    return "O" in arg[1:] or (sub in GIT_LOWER_O_SUBCOMMANDS and "o" in arg[1:])


def _git_runs_commands(args: List[str]) -> bool:
    """git -c / --config-env / --exec-path (core.fsmonitor, core.pager, diff.external...) or a sub-command option
    that writes a file or runs a command (log/show/diff --output=F, grep -O<cmd> / -nOrm / --op=<cmd>, --ext-diff,
    fetch --upload-pack / --upl=, push --receive-pack / --exec), abbreviations included."""
    i = 0
    while i < len(args) and args[i].startswith("-"):
        if args[i] == "-c" or args[i].startswith(GIT_RUN_GLOBAL_OPTIONS):
            return True
        i += 2 if args[i] in GIT_GLOBAL_VALUE_OPTIONS else 1
    sub = args[i].lower() if i < len(args) else ""
    return any(_git_long_options(a) or _git_short_cluster_runs(sub, a) for a in args[i + 1:])


def _git_exec_options(args: List[str]) -> Tuple[List[str], List[str]]:
    """(shell-command values, operands the commands act on) of git's command-running sub-command options:
    grep -O<cmd> / -nOrm / --open-files-in-pager[=cmd] (abbreviations: --op=) run the pager on the matched files,
    whose operands default to '.'; fetch/ls-remote --upload-pack, push --receive-pack / --exec (--upl=, --rece=,
    --exe=) run on a local repository operand (git fetch --upl='rm -rf logs;:' .). Operands are [] when no such
    option is present; patterns (-e .) are kept as operands, which errs toward denying."""
    sub, rest = _git_subcommand(args)
    short_exec = GIT_SUBCOMMAND_SHORT_EXEC.get(sub, set())
    short_maybe = GIT_SUBCOMMAND_SHORT_EXEC_MAYBE.get(sub, set())
    runs = False
    values: List[str] = []
    positionals: List[str] = []
    post_dash: List[str] = []
    saw_dash = pattern_given = skip = False
    for j, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a == "--":
            saw_dash = True
            post_dash += rest[j + 1:]
            break
        names = [n for n in _git_long_options(a) if n in GIT_EXEC_LONG_OPTIONS]
        if names:
            runs = True
            if "=" in a:
                values.append(a.split("=", 1)[1])
            elif any(n in GIT_EXEC_VALUE_OPTIONS for n in names) and j + 1 < len(rest):
                values.append(rest[j + 1])
                skip = True
            continue
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if sub == "grep":
                # value-taking long options, unique prefixes included (--max-count 1, --threads 2, --thr 2)
                key = _resolve_long(name, GIT_GREP_LONG_OPTIONS)
                if "=" not in a and GIT_GREP_LONG_OPTIONS.get(key) is True and j + 1 < len(rest):
                    skip = True
            continue
        if re.match(r"^-[^-]", a):
            if sub == "grep":
                cl = _git_grep_cluster(a)
                if cl["runs"]:
                    runs = True
                    if cl["pager"]:
                        values.append(cl["pager"])
                pattern_given = pattern_given or cl["pattern"]
                if cl["takes_next"] and j + 1 < len(rest):
                    skip = True
                continue
            letters = a[1:]
            alias = next((c for c in letters if c in short_exec | short_maybe), "")
            if alias:                       # short alias of a command-running option (clone -u, rebase -x)
                runs = True
                glued = letters[letters.index(alias) + 1:]
                if glued:
                    values.append(glued)
                elif j + 1 < len(rest):
                    values.append(rest[j + 1])
                    skip = alias in short_exec  # fetch / pull -u: judged, and still parsed as an operand
            elif "O" in letters:            # diff/log/show -O<orderfile> only reads a file
                runs = runs or sub == "grep"
                if a[a.index("O") + 1:]:
                    values.append(a[a.index("O") + 1:])
            continue
        positionals.append(a)
    if not runs:
        return values, []
    if sub == "grep":
        operands = post_dash if saw_dash else (positionals if pattern_given else positionals[1:])
        if not operands:
            operands = ["."]  # only a pattern: git grep searches the working directory
    else:
        operands = positionals + post_dash
    return values, operands


def _rg_exec_options(args: List[str]) -> Tuple[List[str], List[str]]:
    """(command values, operands) of rg --pre CMD (run on every searched file, operands default to '.') and
    --hostname-bin CMD; operands are [] without --pre. The pattern is kept as an operand (errs toward denying)."""
    pre = False
    values: List[str] = []
    operands: List[str] = []
    skip = False
    for j, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--":
            operands += args[j + 1:]
            break
        if a in RG_EXEC_OPTIONS:
            pre = pre or a == "--pre"
            if j + 1 < len(args):
                values.append(args[j + 1])
                skip = True
        elif a.startswith(tuple(o + "=" for o in RG_EXEC_OPTIONS)):
            pre = pre or a.startswith("--pre=")
            values.append(a.split("=", 1)[1])
        elif a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if "=" not in a and name in RG_VALUE_LONG and j + 1 < len(args):
                skip = True  # skip the value of a value-taking long option (--max-depth 2, --threads 2)
        elif a.startswith("-") and len(a) > 1:
            if _short_cluster_takes_next(a[1:], RG_VALUE_SHORT) and j + 1 < len(args):
                skip = True  # skip the value of a value-taking short option (-m 1, -t json, -A 2)
        else:
            operands.append(a)
    if not pre:
        return values, []
    return values, operands + (["."] if len(operands) <= 1 else [])


def _command_option_operands(prog: str, args: List[str]) -> List[str]:
    """Operands of an allowlisted program whose command-running option acts on them (rg --pre, git grep -O,
    git fetch --upload-pack ...); [] when no such option is present."""
    if prog == "rg":
        return _rg_exec_options(args)[1]
    if prog == "git":
        return _git_exec_options(args)[1]
    return []


def _git_wipes_logs(args: List[str]) -> bool:
    """git clean -x/-X and git stash --all delete the ignored runtime state under logs/."""
    sub, rest = _git_subcommand(args)
    if sub == "clean":
        return any(re.match(r"^-[A-Za-z]*[xX]", a) for a in rest)
    if sub == "stash":
        return any(a == "--all" or re.match(r"^-[A-Za-z]*a", a) for a in rest)
    return False


def _shell_args(args: List[str]) -> Dict[str, Any]:
    """How a shell invocation (sh / bash / zsh / dash / ksh / fish ...) runs code: {command: the -c string or None,
    script: the script operand or None, stdin: reads its commands from stdin (-s, or no script operand), info:
    --version / --help only, rcfiles: --rcfile / --init-file}. Options are parsed with their values (-o opt,
    +O opt, clusters such as -eo pipefail, --norc, --login)."""
    out: Dict[str, Any] = {"command": None, "script": None, "stdin": False, "info": False, "rcfiles": []}
    has_c = has_s = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--", "-"):
            i += 1
            break
        if a.startswith("--"):
            opt, eq, value = a[2:].partition("=")
            if opt in ("rcfile", "init-file", "command"):
                if not eq and i + 1 < len(args):
                    value, i = args[i + 1], i + 1
                if opt == "command":
                    out["command"] = value
                else:
                    out["rcfiles"].append(value)
            elif opt in ("version", "help"):
                out["info"] = True
            i += 1
            continue
        if a[:1] in "-+" and len(a) > 1:
            letters = a[1:]
            has_c = has_c or "c" in letters
            has_s = has_s or "s" in letters
            i += 1 + sum(1 for ch in letters if ch in "oO")  # -o / -O / +o take the next word
            continue
        break
    rest = args[i:]
    if out["command"] is not None:
        return out
    if has_c:
        out["command"] = rest[0] if rest else ""
    elif has_s or not rest:
        out["stdin"] = True
    else:
        out["script"] = rest[0]
    return out


def _ps_param(arg: str) -> str:
    """PowerShell CLI parameter name: leading '-', '--' or '/' stripped, lower-case, ':value' dropped."""
    m = re.match(r"^(?:--?|/)([A-Za-z][\w-]*)", arg or "")
    return m.group(1).lower() if m else ""


def _ps_encoded_param(arg: str) -> bool:
    """powershell / pwsh -EncodedCommand in any spelling PowerShell accepts: -e, -ec, -en, -enc, ..., -encodedc,
    /enc, --enc (any prefix of encodedcommand: fail-safe). -EncodedArguments / -ea is not a command."""
    p = _ps_param(arg)
    return bool(p) and ("encodedcommand".startswith(p) or p == "ec")


def _ps_command_param(arg: str) -> bool:
    """powershell / pwsh -Command in any prefix spelling (-c, -com, /command, --command)."""
    p = _ps_param(arg)
    return bool(p) and "command".startswith(p)


def _ps_param_value(args: List[str], i: int) -> Optional[str]:
    """Value of the PowerShell parameter args[i]: glued after ':' (-enc:BASE64) or the next word."""
    a = args[i]
    if ":" in a[1:]:
        return a.split(":", 1)[1]
    return args[i + 1] if i + 1 < len(args) else None


def _call_literals(code: str, start: int) -> List[str]:
    """String literals of a call's arguments, from `start` to its closing bracket or the end of the statement."""
    parts: List[str] = []
    depth = 0
    i, n = start, min(len(code), start + 2000)
    while i < n:
        ch = code[i]
        if ch in "'\"":
            j = code.find(ch, i + 1)
            if j < 0:
                break
            parts.append(code[i + 1:j])
            i = j + 1
            continue
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            if depth == 0:
                break
            depth -= 1
        elif ch in ";\n" and depth == 0:
            break
        i += 1
    return parts


def _inline_shell_strings(code: str) -> List[str]:
    """Shell command lines inline code hands to a shell: the string literals of os.system / subprocess.* / popen /
    exec* / spawn* / child_process.exec* / perl system / qx{} / ruby %x() / awk system() calls, joined
    (subprocess.run(['rm', '-rf', 'logs']) -> 'rm -rf logs')."""
    code = (code or "").replace('\\"', '"').replace("\\'", "'")
    out: List[str] = []
    for m in INLINE_SHELL_CALL_RE.finditer(code):
        parts = _call_literals(code, m.end())
        if parts:
            out.append(" ".join(parts))
    out += [m.group(2) for m in INLINE_QX_RE.finditer(code)]
    return out


def _nested_commands(prog: str, args: List[str]) -> List[str]:
    """Command strings a sub-command runs through another shell: sh/bash -c '...', eval ..., cmd /c ...,
    powershell -Command / -EncodedCommand ..., git -c values (core.fsmonitor='rm -rf logs', alias.x='!cmd'), git
    bisect run / submodule foreach, the values of command-running options (git grep -O<cmd> / --op=, fetch
    --upload-pack / --upl=, push --receive-pack / --exec, rg --pre / --hostname-bin, less +!cmd / +|<mark>cmd, tar
    --to-command / -I / -F / --checkpoint-action=exec=), watch, su / runuser / script / flock -c, and the shell
    calls of inline interpreter code (os.system('...'), awk system("..."))."""
    if prog == "git":
        values = []
        for i, a in enumerate(args[:-1]):
            if a == "-c" and "=" in args[i + 1]:
                values.append(args[i + 1].split("=", 1)[1].lstrip("!"))
            elif not a.startswith("-") and (i == 0 or args[i - 1] not in GIT_GLOBAL_VALUE_OPTIONS):
                break  # the sub-command: its own -c options (git grep -c) are not config values
        nested = values + _git_exec_options(args)[0]
        sub, rest = _git_subcommand(args)
        # Sub-commands that run the rest of their argv as a shell command
        if sub == "bisect" and rest[:1] == ["run"] and rest[1:]:
            nested.append(" ".join(rest[1:]))
        if sub == "submodule" and "foreach" in rest:
            k = rest.index("foreach") + 1
            while k < len(rest) and rest[k] in ("--recursive", "--quiet", "-q"):
                k += 1
            if rest[k:]:
                nested.append(" ".join(rest[k:]))
        return nested
    if prog == "rg":
        return _rg_exec_options(args)[0]
    if prog == "less":
        values = []
        for a in args:
            m = re.search(r"!(.*)|\|.(.*)", a[1:], re.DOTALL) if a.startswith("+") else None
            if m:
                values.append(m.group(1) if m.group(1) is not None else m.group(2))
        return values
    if prog in SHELL_INTERPRETERS:
        command = _shell_args(args)["command"]
        return [command] if command is not None else []
    if prog == "eval":
        return [" ".join(args)] if args else []
    if prog == "cmd":
        return next(([" ".join(args[i + 1:])] for i, a in enumerate(args) if re.match(r"^/+[cCkK]$", a)), [])
    if prog in ("powershell", "pwsh"):
        out: List[str] = []
        for i, a in enumerate(args):
            if _ps_command_param(a):
                return out + ([" ".join(args[i + 1:])] if i + 1 < len(args) else [])
            if _ps_encoded_param(a):
                value = _ps_param_value(args, i)
                decoded = _decode_ps_encoded(value) if value else None  # base64 UTF-16LE; judged like -Command
                if decoded is not None:
                    out.append(decoded)
        return out
    if prog == "watch":  # watch [-n SECS] [-q N] ... cmd args: run through `sh -c`
        i = 0
        while i < len(args) and args[i].startswith("-") and args[i] != "--":
            i += 2 if re.match(r"^-[A-Za-z]*[nq]$", args[i]) or args[i] in ("--interval", "--equexit") else 1
        if i < len(args) and args[i] == "--":
            i += 1
        return [" ".join(args[i:])] if args[i:] else []
    if prog in ("su", "runuser", "script", "flock"):
        out = []
        for i, a in enumerate(args):
            if a in ("-c", "--command") and i + 1 < len(args):
                out.append(args[i + 1])
            elif a.startswith("--command="):
                out.append(a.split("=", 1)[1])
        return out
    if prog in ("tar", "bsdtar"):
        return _tar_parse(args)["nested"]
    if SHELL_CALL_INTERPRETERS_RE.match(prog):
        return _inline_shell_strings(" ".join(args))
    return []


def _decode_ps_encoded(value: str) -> Optional[str]:
    """Decodes a PowerShell -EncodedCommand payload (base64 of UTF-16LE). Returns None when undecodable."""
    import base64
    try:
        return base64.b64decode(value, validate=False).decode("utf-16-le")
    except Exception:
        return None


def _tar_parse(args: List[str]) -> Dict[str, Any]:
    """GNU tar / bsdtar arguments: {extract (x, --extract, --get), stdout (O, --to-stdout), dests (-C dir,
    --directory=dir), nested (--to-command, -I / --use-compress-program, -F / --info-script, --rsh-command,
    --rmt-command, --checkpoint-action=exec=CMD), outputs (--index-file, -g / --listed-incremental, --volno-file)}.
    The old-style first argument is a bundle (tar xzf a.tgz: the values of f / C ... follow in order); short
    clusters stop at the first value-taking letter (tar -czf /tmp/o.tgz index.txt only creates)."""
    out: Dict[str, Any] = {"extract": False, "stdout": False, "dests": [], "nested": [], "outputs": []}
    letters: List[Tuple[str, Optional[str]]] = []
    n = len(args)
    i = 0
    if args and not args[0].startswith("-"):
        i = 1
        for ch in args[0]:
            if ch in TAR_VALUE_SHORT:
                letters.append((ch, args[i] if i < n else ""))
                i += 1
            else:
                letters.append((ch, None))
    while i < n:
        a = args[i]
        i += 1
        if a == "--":
            break
        if a.startswith("--"):
            opt, eq, value = a[2:].partition("=")
            opt = opt.lower()
            if not eq and len(opt) >= 2 and any(o.startswith(opt) for o in TAR_VALUE_LONG) and i < n:
                value, i = args[i], i + 1
            if opt == "get" or (len(opt) >= 3 and "extract".startswith(opt)):
                out["extract"] = True
            if len(opt) >= 4 and "to-stdout".startswith(opt):
                out["stdout"] = True
            if len(opt) >= 3 and "directory".startswith(opt):
                out["dests"].append(value)
            if len(opt) >= 2 and any(o.startswith(opt) for o in TAR_EXEC_LONG):
                out["nested"].append(value)
            if opt.startswith("checkpoint-") and value.lower().startswith("exec="):
                out["nested"].append(value[5:])
            if len(opt) >= 3 and any(o.startswith(opt) for o in TAR_OUTPUT_LONG):
                out["outputs"].append(value)
            continue
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch in TAR_VALUE_SHORT:
                    value = a[k + 2:]
                    if not value and i < n:
                        value, i = args[i], i + 1
                    letters.append((ch, value))
                    break
                letters.append((ch, None))
    for ch, value in letters:
        if ch == "x":
            out["extract"] = True
        elif ch == "O":
            out["stdout"] = True
        elif ch == "C":
            out["dests"].append(value or "")
        elif ch in TAR_EXEC_SHORT:
            out["nested"].append(value or "")
        elif ch == "g":
            out["outputs"].append(value or "")
    return out


def _archive_extracts(prog: str, args: List[str]) -> bool:
    """True when an archive command extracts files to disk (not tar -t / -xO, unzip -l / -p ...)."""
    if prog in ("tar", "bsdtar"):
        parsed = _tar_parse(args)
        return parsed["extract"] and not parsed["stdout"]
    if prog == "unzip":
        return not any(a in ("-l", "-t", "-v", "-z", "-Z", "-p") for a in args)
    if prog in ("7z", "7za", "7zr"):
        pos = [a for a in args if not a.startswith("-")]
        return bool(pos) and pos[0].lower() in ("x", "e")
    if prog == "expand-archive":
        return True
    return False


def _archive_dests(prog: str, args: List[str]) -> List[str]:
    """Destination directories of an extraction (tar -C / --directory, unzip -d, 7z -o<dir>, Expand-Archive
    -DestinationPath or its 2nd positional); [''] = the cwd."""
    dests: List[str] = []
    if prog in ("tar", "bsdtar"):
        dests = _tar_parse(args)["dests"]
    elif prog == "unzip":
        for i, a in enumerate(args):
            if a == "-d" and i + 1 < len(args):
                dests.append(args[i + 1])
            elif re.match(r"^-d.", a):
                dests.append(a[2:])
    elif prog in ("7z", "7za", "7zr"):
        dests = [a[2:] for a in args if re.match(r"^-o.", a)]
    elif prog == "expand-archive":
        positional: List[str] = []
        i = 0
        while i < len(args):
            a = args[i]
            p = _ps_param(a) if a.startswith("-") else ""
            if p:
                value = a.split(":", 1)[1] if ":" in a else None
                takes = any(x.startswith(p) for x in ("destinationpath", "path", "literalpath"))
                if takes and value is None and i + 1 < len(args):
                    value, i = args[i + 1], i + 1
                if "destinationpath".startswith(p):
                    dests.append(value or "")
            else:
                positional.append(a)
            i += 1
        dests += positional[1:2]
    return dests or [""]


def _windows_copy_parse(prog: str, args: List[str]) -> Tuple[List[str], Optional[str], bool]:
    """(sources, destination or None = the cwd, recursive) of robocopy / xcopy / cmd copy / Copy-Item. robocopy
    copies the contents of a directory (/S /E /MIR recurse; /PURGE /MOV delete); Copy-Item recurses with -Recurse
    (parameters by unique prefix: -Path / -LiteralPath / -Destination, or positional)."""
    if prog in ("robocopy", "xcopy", "copy"):
        switches = [a.lower().split(":", 1)[0] for a in args if WINDOWS_COPY_SWITCH_RE.match(a)]
        ops = [a for a in args if not WINDOWS_COPY_SWITCH_RE.match(a) and a != "+"]
        if prog == "copy":
            return (ops[:-1] if len(ops) > 1 else ops), (ops[-1] if len(ops) > 1 else None), False
        recursive = any(s in WINDOWS_RECURSIVE_SWITCHES for s in switches)
        return ops[:1] + ops[2:], (ops[1] if len(ops) > 1 else None), recursive
    sources: List[str] = []
    dest: Optional[str] = None
    recursive = False
    positional: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        p = _ps_param(a) if a.startswith("-") else ""
        if p:
            value = a.split(":", 1)[1] if ":" in a else None
            takes = any(x.startswith(p) for x in ("path", "literalpath", "destination") + PS_COPY_VALUE_PARAMS)
            if takes and value is None and i + 1 < len(args):
                value, i = args[i + 1], i + 1
            if "destination".startswith(p):
                dest = value
            elif "path".startswith(p) or "literalpath".startswith(p):
                sources += [s for s in (value or "").split(",") if s]
            elif "recurse".startswith(p):
                recursive = True
        else:
            positional.append(a)
        i += 1
    if not sources and positional:
        sources, positional = positional[:1], positional[1:]
    if dest is None and positional:
        dest = positional[0]
    return sources, dest, recursive


def _unresolved(word: Optional[str]) -> bool:
    """A word whose value is only known at run time ($VAR, ${VAR}, $(...) lifted to $__subst__, `...`)."""
    return "$" in (word or "") or "`" in (word or "")


def _relative_word(word: str) -> bool:
    """A path operand resolved against the working directory (not /x, C:\\x, ~/x)."""
    w = _word_value(word or "").replace("\\", "/")
    return bool(w) and not w.startswith(("/", "~")) and not re.match(r"^[A-Za-z]:", w)


def _inplace_files(prog: str, args: List[str]) -> List[str]:
    """Files edited in place by sed -i / perl -i: the operands after the sed script / perl code (given with -e / -f /
    --expression, else the first operand). -i[SUFFIX] ends a short cluster (the rest is the backup suffix)."""
    files: List[str] = []
    code_given = skip = False
    value_letters = "efl" if prog == "sed" else "eEMmI"
    for a in args:
        if skip:
            skip = False
            continue
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0]
            if name in ("expression", "file", "line-length"):
                code_given = code_given or name != "line-length"
                skip = "=" not in a
            continue
        if a.startswith("-") and len(a) > 1:
            for k, ch in enumerate(a[1:]):
                if ch == "i":
                    break
                if ch in value_letters:
                    code_given = code_given or ch in "efE"
                    skip = not a[k + 2:]
                    break
            continue
        files.append(a)
    return files if code_given else files[1:]


def _write_targets(prog: str, args: List[str]) -> List[str]:
    """Operands a write / destructive program creates, overwrites, moves or deletes: every operand of rm / mv /
    touch / tee ..., the destination of cp / install / ln / rsync (and its sources with --remove-source-files),
    dd of=, the files of sed -i / perl -i, and Windows delete / move operands or copy destinations."""
    if prog == "dd":
        return [a[3:] for a in args if a.startswith("of=")]
    if prog in ("sed", "perl"):
        return _inplace_files(prog, args)
    if prog in WINDOWS_COPY_PROGRAMS:
        dest = _windows_copy_parse(prog, args)[1]
        return [dest or "."]
    operands, target = _split_operands(prog, args)
    if prog in WINDOWS_DELETE_PROGRAMS | WINDOWS_MOVE_PROGRAMS:
        operands = [o for o in operands if not WINDOWS_COPY_SWITCH_RE.match(o)]
    if prog in ("cp", "install", "ln", "rsync") and not (prog == "rsync" and any(
            a.startswith("--remove-source") for a in args)):
        return [target] if target is not None else operands[-1:]
    return operands + ([target] if target is not None else [])


def _writes_relative_operand(prog: str, args: List[str], redirects: List[str]) -> bool:
    """A redirect or a write / destructive operand given as a relative path (judged when the working directory is
    inside logs/ or unknown)."""
    if any(_relative_word(t) for t in redirects):
        return True
    if not _is_destructive_write(prog, args):
        return False
    return any(_relative_word(w) for w in _write_targets(prog, args) if not w.startswith("-"))


def _unresolved_write(prog: str, prog_token: str, args: List[str], redirects: List[str], cwd: str,
                      base_dir: str) -> bool:
    """A value only known at run time in a write position: a redirect target, a write / destructive operand, an
    output option (dd of=, --output=, curl -o, sort -o, tar -C ...), the root of a destructive find, an archive
    destination, or the program itself ($RM, $(which rm)) acting on logs/, a protected file or another run-time
    value."""
    if any(_unresolved(t) for t in redirects):
        return True
    if _unresolved(prog_token):
        return any(_unresolved(a) or _reaches_logs_dir(a, cwd, base_dir) or _ground_truth_named(a)
                   or _glob_ground_truth(a) for a in args if not a.startswith("-"))
    if _is_destructive_write(prog, args) and any(_unresolved(w) for w in _write_targets(prog, args)):
        return True
    options = OUTPUT_OPTIONS.get(prog, set())
    for j, a in enumerate(args):
        m = OUTPUT_VALUE_RE.match(a)
        if m and _unresolved(a[m.end():]):
            return True
        if a in options and j + 1 < len(args) and _unresolved(args[j + 1]):
            return True
        if _unresolved(a) and any(len(o) == 2 and a.startswith(o) and len(a) > 2 for o in options):
            return True
    if prog == "find" and _find_is_destructive(args) and any(_unresolved(r) for r in _find_roots(args)):
        return True
    if prog in ARCHIVE_EXTRACT_PROGRAMS and _archive_extracts(prog, args):
        return any(_unresolved(d) for d in _archive_dests(prog, args))
    return False


def _find_is_destructive(args: List[str]) -> bool:
    """find with -delete, an output action (-fprint ...) or an -exec / -ok command that can modify files."""
    if any(a in FIND_DELETE_ACTIONS or a in FIND_OUTPUT_ACTIONS for a in args):
        return True
    for i, a in enumerate(args):
        if a in FIND_EXEC_ACTIONS:
            end = next((j for j in range(i + 1, len(args)) if args[j] in (";", "+")), len(args))
            if _exec_writes(args[i + 1:end]):
                return True
    return False


def _script_targets(prog: str, prog_token: str, args: List[str], shell: str) -> List[Tuple[str, bool]]:
    """Script files a sub-command runs as shell code: (word, by_path). A shell's script operand and --rcfile (bash
    -o errexit f, sh -e -x f, bash --norc f), source f / . f, and (Bash only) a program invoked by path (./evil,
    /tmp/evil, scripts/x, f.sh), which is judged only when it is a text file with a shell or no shebang."""
    if prog in SHELL_INTERPRETERS:
        parsed = _shell_args(args)
        out = [(f, False) for f in parsed["rcfiles"]]
        if parsed["script"] and parsed["script"] not in STDIN_SCRIPT_PATHS:
            out.append((parsed["script"], False))
        return out
    if prog in (".", "source"):
        operand = next((a for a in args if not a.startswith("-")), None)
        return [(operand, False)] if operand and operand not in STDIN_SCRIPT_PATHS else []
    # (a backslash is an escape in Bash, not a separator: only '/' or a C:\ drive path makes a path)
    if shell == "bash" and prog_token and ("/" in prog_token or re.match(r"^[A-Za-z]:[\\/]", prog_token)
                                           or prog.endswith(".sh")):
        return [(prog_token, True)]
    return []


def _is_shell_script_data(data: bytes) -> bool:
    """A file the shell would run as shell code: text (no NUL byte) with no shebang, or a #! sh / bash / zsh / dash
    / ksh ... interpreter (also through /usr/bin/env [-S] bash)."""
    head = data[:4096]
    if b"\0" in head:
        return False
    if not head.startswith(b"#!"):
        return True
    words = head[2:].split(b"\n", 1)[0].decode("utf-8", "replace").split()
    if not words:
        return True
    interpreter = os.path.basename(words[0])
    if interpreter == "env":
        rest = [w for w in words[1:] if not w.startswith("-") and "=" not in w]
        interpreter = os.path.basename(rest[0]) if rest else ""
    return interpreter in SHELL_INTERPRETERS


def _pinned_desk_script(host_path: str, base_dir: str, data: bytes) -> bool:
    """True only for a DESK_SHELL_SCRIPTS file (path resolved against the workspace root) whose bytes, as read by
    the hook, match the pinned sha256: any other script under scripts/, or an edited copy, is judged strictly."""
    try:
        real = os.path.normcase(os.path.realpath(host_path))
        for rel, digest in DESK_SHELL_SCRIPTS.items():
            if real == os.path.normcase(os.path.realpath(os.path.join(base_dir, *rel.split("/")))):
                return hashlib.sha256(data).hexdigest() == digest
    except (OSError, ValueError):
        return False
    return False


def _strip_comment_lines(content: str) -> str:
    """Script text without its whole-line comments (a quote in a comment would unbalance the tokenizer)."""
    return "\n".join("" if line.lstrip().startswith("#") else line for line in content.split("\n"))


def _existing_file(word: str, cwd: str, base_dir: str) -> bool:
    try:
        return os.path.isfile(_host_path(word, _host_path(cwd, base_dir) if cwd else base_dir))
    except (OSError, ValueError):
        return False


def _judge_script(word: str, cwd: str, base_dir: str, depth: int, cwd_unknown: bool, strict: bool,
                  written: frozenset, by_path: bool = False) -> List[str]:
    """Protected files a shell script file can write: its content is judged with the full line analysis, one
    nesting level deeper (bounded by NESTED_DEPTH_LIMIT, so self-sourcing scripts deny). Denied outright when the same
    command line writes it (strict), when its path is only known at run time or relative to an unknown cwd
    (strict), and when it is larger than SCRIPT_READ_LIMIT, unreadable or not UTF-8. A missing file judges as
    nothing. Only the first SCRIPT_HEAD_BYTES are read to classify a program invoked by path; the rest only when it
    is a shell script. The content is judged strictly, except a sha256-pinned DESK_SHELL_SCRIPTS file (run-time
    values / unknown cwd skipped); scripts it sources or runs are strict again. Memoised within an audit scope."""
    key = ("script", word, cwd, base_dir, depth, cwd_unknown, strict, written, by_path)
    memo = _AUDIT["memo"] if _AUDIT["active"] else None
    if memo is not None and key in memo:
        return memo[key]
    result = _judge_script_uncached(word, cwd, base_dir, depth, cwd_unknown, strict, written, by_path)
    if memo is not None:
        memo[key] = result
    return result


def _judge_script_uncached(word: str, cwd: str, base_dir: str, depth: int, cwd_unknown: bool, strict: bool,
                           written: frozenset, by_path: bool) -> List[str]:
    deny = list(GROUND_TRUTH_FILES)
    if not word:
        return []
    if strict and (posixpath.basename(_shell_path(word)).lower() in written or "*" in written):
        return deny
    if _unresolved(word) or (cwd_unknown and _relative_word(word)):
        return deny if strict else []
    try:
        host = _host_path(word, _host_path(cwd, base_dir) if cwd else base_dir)
        if not os.path.isfile(host):
            return []
        size = os.path.getsize(host)
        with open(host, "rb") as fh:
            data = fh.read(SCRIPT_HEAD_BYTES)
            if by_path and not _is_shell_script_data(data):
                return []  # a binary, or a script for another interpreter (Python / Node files: residual)
            if size > SCRIPT_READ_LIMIT:
                return deny
            data += fh.read(SCRIPT_READ_LIMIT + 1 - len(data))
    except (OSError, ValueError):
        return deny
    if len(data) > SCRIPT_READ_LIMIT:
        return deny
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        return deny
    return _ground_truth_line_hits(_strip_comment_lines(content), cwd, base_dir, depth=depth + 1,
                                   cwd_unknown=cwd_unknown, strict=not _pinned_desk_script(host, base_dir, data),
                                   written=written)


def _git_exec_config_writes(prog: str, args: List[str], redirects: List[str], cwd: str, assigned: bool,
                            read_programs: frozenset) -> bool:
    """A sub-command (not git itself) that writes a git config / hook file (GIT_EXEC_CONFIG_PATH_RE): a redirect
    target, the write targets of a writer (tee, cp / install / rsync / mv destination, chmod / touch, dd of=, sed -i,
    Windows copies / moves), EVERY operand of a link (ln, link, cp -l / -s / --link / --symbolic-link, rsync
    --link-dest: a link aliases its source), an output option (curl -o, --output=), an archive extraction
    destination or a tar output file; relative words are also judged against the tracked cwd (cd .git/hooks && echo
    x > pre-commit). Pure deletions (rm, unlink, shred, del ...) plant nothing and are left alone. Catch-all (Bash
    and PowerShell): any other program outside the read-only allowlist and GIT_EXEC_CONFIG_MODELLED_PROGRAMS naming
    such a path in an argument (patch, sponge, ed, vim -c wq, awk -i inplace, perl -e 'open(F,q(>>.git/config))',
    Set-Content, Out-File ...)."""
    words = list(redirects)
    operands, target = _split_operands(prog, args)
    link = prog in ("ln", "link") or (prog == "cp" and any(
        a in CP_LINK_LONG_OPTIONS or (re.match(r"^-[A-Za-z]+$", a) and re.search(r"[ls]", a)) for a in args))
    if link:
        words += operands + ([target] if target is not None else [])
    elif prog == "mv":  # its destination only: moving a config / hook away plants nothing
        words += [target] if target is not None else operands[-1:]
    elif _is_destructive_write(prog, args) and prog not in GIT_EXEC_CONFIG_DELETE_PROGRAMS:
        words += _write_targets(prog, args)
    if prog == "rsync":
        words += [a.split("=", 1)[1] for a in args if a.startswith("--link-dest=")]
    options = OUTPUT_OPTIONS.get(prog, set())
    for j, a in enumerate(args):
        m = OUTPUT_VALUE_RE.match(a)
        if m:
            words.append(a[m.end():])
        elif a in options and j + 1 < len(args):
            words.append(args[j + 1])
    if prog in ARCHIVE_EXTRACT_PROGRAMS and _archive_extracts(prog, args):
        words += [d or "." for d in _archive_dests(prog, args)]
    if prog in ("tar", "bsdtar"):
        words += _tar_parse(args)["outputs"]
    for w in words:
        if not w:
            continue
        if _git_exec_config_path(w):
            return True
        if cwd and _relative_word(w) and not _unresolved(w) and _git_exec_config_path(posixpath.join(cwd, w)):
            return True
    if (link or prog in GIT_EXEC_CONFIG_MODELLED_PROGRAMS or prog in GIT_EXEC_CONFIG_DELETE_PROGRAMS
            or prog in SHELL_INTERPRETERS or prog in CHDIR_PROGRAMS
            or (prog in GIT_EXEC_CONFIG_READ_PROGRAMS and not assigned
                and not (prog == "xxd" and any(re.match(r"^-[A-Za-z]*r", a) or a.startswith("-rev") for a in args)))
            or _ground_truth_read_only(prog, args, assigned, read_programs)):
        return False
    return any(_git_exec_config_path(a) or any(_git_exec_config_path(w) for w in GIT_PATH_WORD_RE.findall(a))
               for a in args)


def _names_only_own_ground_truth(tokens: List[str], mentioned: List[str], info: dict, cwd: str,
                                 base_dir: str) -> bool:
    """True when a sub-command runs a READ_ONLY_SCRIPTS script by its exact repo path (never a linked worktree copy)
    and every ground-truth file it names is one of that script's own outputs, of which it is the sole sanctioned
    writer (scripts/trade_outcomes.py -> logs/trade_outcomes.jsonl, scripts/trading_scorecard.py ->
    logs/score_calibration.json; issue #191). Only RISK_ENV_ASSIGNMENTS may precede it. Any other program, script or
    named protected file (another script's output, a glob reaching other files) keeps the ground-truth denial."""
    if any(var not in RISK_ENV_ASSIGNMENTS for var, _value in info.get("assigns") or []):
        return False
    key = _read_only_script_key(tokens, cwd, base_dir)
    return bool(key) and set(mentioned) <= set(READ_ONLY_SCRIPTS[key][2])


def _ground_truth_writes(tokens: List[str], text: str, cwd: str = "", base_dir: str = "", depth: int = 0,
                         shell: str = "bash", cwd_unknown: bool = False, strict: bool = True,
                         written: frozenset = frozenset(), cwd_set: Tuple[str, ...] = ()) -> List[str]:
    """Protected ground-truth files a single shell sub-command can create, modify, move, delete or alias.
    shell="powershell" (this sub-command only; nested shells and find -exec are Bash): PS_READ_CMDLETS also read,
    `wsl.exe [options] [--] <linux command>` is judged as that Linux command, and the Bash-only rules for run-time
    values / unknown cwd / scripts by path do not apply. cwd_unknown: an earlier cd target could not be resolved.
    strict=False (only the sha256-pinned DESK_SHELL_SCRIPTS) skips the run-time-value and unknown-cwd rules. written:
    basenames of files the enclosing command lines write (a script run from one of them is denied). cwd_set: every
    possible cwd of the enclosing line (nested lines start from all of them). Counts against the audit budget."""
    _audit_tick()
    if depth > NESTED_DEPTH_LIMIT:
        return list(GROUND_TRUTH_FILES)  # pathological nesting: fail closed
    all_files = list(GROUND_TRUTH_FILES)
    cwd = cwd or base_dir
    bash_rules = strict and shell == "bash"
    hits: List[str] = []
    idx, info = _command_start(tokens)
    prog_token = tokens[idx] if idx < len(tokens) else ""
    prog = re.sub(r"\.exe$", "", os.path.basename(prog_token).lower())
    args = _plain_args(tokens[idx + 1:] if idx < len(tokens) else [])
    redirects = _redirect_targets(tokens) + info["outputs"]
    for target in redirects:
        hits += _ground_truth_named(target) + _glob_ground_truth(target)
    # wsl.exe [options] [-e | --] <cmd> (Bash and PowerShell): <cmd> runs in Linux, judged with its own tokens (from
    # an unknown cwd after --cd). A Bash line then also goes through every rule below with its outer tokens (VAR=v
    # wsl.exe ..., redirects, run-time values), as before the unwrap; PowerShell keeps its per-cmdlet rules only.
    if prog == "wsl":
        linux = _wsl_command(args)
        if linux is not None:
            if linux:
                hits += _ground_truth_writes(linux, " ".join(linux), cwd, base_dir, depth + 1,
                                             cwd_unknown=cwd_unknown or _wsl_cd(args) is not None, strict=strict,
                                             written=written, cwd_set=cwd_set)
            if shell == "powershell":
                return _protected_hits(hits)
    # env -C dir / sudo -D dir: this sub-command runs in another directory
    for d in info["chdirs"]:
        if _unresolved(d) or not d or d.startswith(("~", "-")):
            cwd_unknown = True
        else:
            cwd = _join_cwd(cwd, d)
            cwd_unknown = cwd_unknown or _proc_alias(d)
    read_programs = GROUND_TRUTH_READ_PROGRAMS | (PS_READ_CMDLETS if shell == "powershell" else set())
    mentioned = _ground_truth_named(text)
    for a in tokens:
        mentioned += _glob_ground_truth(_word_value(a))
    if mentioned and not _ground_truth_read_only(prog, args, bool(info["assigns"]), frozenset(read_programs)) \
            and not (not cwd_unknown and _names_only_own_ground_truth(tokens, mentioned, info, cwd, base_dir)):
        hits += mentioned
    # Git config / hook files written directly (a later git command runs them); git itself is left to the git
    # config key rules (git config -f .git/config <key> <value>)
    # HOME= / XDG_CONFIG_HOME= in front of git (prefix or env) move its global config, --git-dir its repository
    if prog == "git" and (any(var in GIT_HOME_ENV_VARS for var, _value in info["assigns"]) or _git_dir_option(args)):
        hits.append(GIT_EXEC_CONFIG_KEY)
    if prog != "git" and _git_exec_config_writes(prog, args, redirects, cwd, bool(info["assigns"]),
                                                    frozenset(read_programs)):
        hits.append(GIT_EXEC_CONFIG_KEY)
    # Env-var command channels / config injection: VAR=v cmd, VAR+=v, env / sudo / nice env VAR=v, env -S,
    # export / declare VAR=v, read VAR, printf -v VAR, for VAR in ...
    if prog != "unset":
        for var, value in info["assigns"] + _declared_assignments(prog, args):
            if _env_assignment_denied(var, value, prog, prog_token, info["wrappers"]):
                hits.extend(all_files)
                break
    # Git config that runs commands (-c / --config-env / git config / clone -c / --template) and work-tree writes
    # through -C / --work-tree into logs/
    if prog == "git" and (_git_config_denied(args) or _git_base_dir_write(args, cwd, base_dir, bash_rules)):
        hits.extend(all_files)
    # git's ext:: transport runs its URL as a shell command (git clone 'ext::sh -c rm% -rf% logs' /tmp/y)
    if prog == "git" and any(a.lower().startswith("ext::") for a in args):
        hits.extend(all_files)
    # PowerShell -EncodedCommand (any prefix spelling) with a missing or undecodable payload: fail closed
    if prog in ("powershell", "pwsh"):
        for i, a in enumerate(args):
            if _ps_encoded_param(a):
                value = _ps_param_value(args, i)
                if not value or _decode_ps_encoded(value) is None:
                    hits.extend(all_files)
    # A relative write (operand or redirect) from inside logs/ or from an unknown directory (cd "$X"; rm x.json)
    if ((bash_rules and cwd_unknown) or _cwd_in_logs(cwd, base_dir)) and _writes_relative_operand(prog, args,
                                                                                               redirects):
        hits.extend(all_files)
    # Run-time values ($VAR, $(...), `...`) in a write position
    if bash_rules and _unresolved_write(prog, prog_token, args, redirects, cwd, base_dir):
        hits.extend(all_files)
    # A write / destructive operand through /proc/<pid>/cwd|root|fd or /dev/fd: it resolves against the command's
    # process, not the hook's (rm -f /proc/self/cwd/logs/x, mv /proc/self/cwd/logs /tmp/x)
    if _is_destructive_write(prog, args) and any(_proc_alias(w) for w in _write_targets(prog, args)):
        hits.extend(all_files)
    # Nested command lines (bash -c, eval, cmd /c, powershell -Command / -EncodedCommand, git -c values, rg --pre,
    # env -S, flock -c, watch, tar --to-command, os.system('...') ...): the full, strict line analysis one level
    # deeper, started from every possible cwd at once (memoised: the same text is judged once per cwd set)
    nested_cwds = [cwd] if info["chdirs"] or not cwd_set else list(cwd_set)
    for nested in _nested_commands(prog, args) + info["nested"]:
        hits += _ground_truth_line_hits(nested, cwd, base_dir, depth=depth + 1, cwd_unknown=cwd_unknown,
                                        written=written, cwds=nested_cwds)
    # Shell script files (bash f, sh -e f, source f, ./f with a shell or no shebang): their content, same analysis
    for word, by_path in _script_targets(prog, prog_token, args, shell):
        hits += _judge_script(word, cwd, base_dir, depth, cwd_unknown, True, written, by_path)  # always strict
    # A command-running option over logs/ or an ancestor (rg --pre rm . logs, git fetch --upl=CMD .): the command
    # runs on the protected files (or the local repository) whatever its value names. `git -C <dir>` changes the
    # base the operands resolve against (git -C logs grep -O x -- '*.json' searches inside logs/).
    op_operands = _command_option_operands(prog, args)
    if prog == "git" and op_operands:
        cdir = _git_c_dir(args)
        if cdir:
            op_operands = op_operands + [cdir]
    if any(_reaches_logs_dir(o, cwd, base_dir) for o in op_operands):
        hits.extend(all_files)
    operands, target_dir = _split_operands(prog, args)
    if prog in WINDOWS_DELETE_PROGRAMS | WINDOWS_MOVE_PROGRAMS:
        operands = [o for o in operands if not WINDOWS_SWITCH_RE.match(o)]  # rd /s /q: switches, not paths
    globbed = any(SHELL_GLOB_RE.search(a) for a in operands)
    recursive = _is_recursive(args) or (prog in WINDOWS_DELETE_PROGRAMS and any(a.lower() == "/s" for a in args))
    if prog in {"rm"} | WINDOWS_DELETE_PROGRAMS and recursive:
        if any(_reaches_logs_dir(a, cwd, base_dir) for a in operands):
            hits.extend(all_files)
    elif (prog in LOGS_DIR_DESTRUCTIVE_PROGRAMS | WINDOWS_DELETE_PROGRAMS
          and any(_is_logs_dir(a, cwd, base_dir) for a in operands)):
        hits.extend(all_files)
    if prog in {"mv"} | WINDOWS_MOVE_PROGRAMS and operands:
        sources, dest = (operands, target_dir) if target_dir is not None else (operands[:-1], operands[-1])
        # Moving the logs directory (or an ancestor) away, or a glob of unseen names into it;
        # `mv report.txt logs/` stays allowed.
        if (any(_reaches_logs_dir(s, cwd, base_dir) for s in sources)
                or (dest and globbed and _is_logs_dir(dest, cwd, base_dir))):
            hits.extend(all_files)
    rsync_files_from = prog == "rsync" and any(a.startswith(("--files-from", "--include-from")) for a in args)
    if prog in LOGS_DIR_COPY_PROGRAMS and operands and (_is_recursive(args) or globbed or rsync_files_from):
        sources, dest = (operands, target_dir) if target_dir is not None else (operands[:-1], operands[-1])
        # Into logs/ (cp -r src/. logs, cp -t logs src/*), a source dir named logs (any glob that can expand to one:
        # /tmp/f/*) into logs/, or a recursive / glob / --files-from copy whose destination is logs/ or one of its
        # ancestors (cp -r /tmp/f/* ., rsync -a /tmp/x/ ./, rsync --files-from=list / .)
        if ((dest and _is_logs_dir(dest, cwd, base_dir))
                or any(_shell_path(s).rpartition("/")[2].lower() == "logs" for s in sources)
                or (dest and _reaches_logs_dir(dest, cwd, base_dir))):
            hits.extend(all_files)
    # Windows / PowerShell copies (robocopy src dest /E, xcopy /S, copy, Copy-Item -Recurse): into logs/ unless it
    # copies existing regular files one by one (copy a.txt logs), into an ancestor of logs/ when recursive
    if prog in WINDOWS_COPY_PROGRAMS:
        sources, dest, win_recursive = _windows_copy_parse(prog, args)
        dest = dest or "."
        if _is_logs_dir(dest, cwd, base_dir):
            if (prog in ("robocopy", "xcopy") or win_recursive or not sources
                    or any(SHELL_GLOB_RE.search(s) or not _existing_file(s, cwd, base_dir) for s in sources)):
                hits.extend(all_files)
        elif win_recursive and _reaches_logs_dir(dest, cwd, base_dir):
            hits.extend(all_files)
    # Archive extraction whose destination directory (-C / -d / -o / -DestinationPath, else the cwd) is logs/ or an
    # ancestor (tar -x -C logs, unzip -d ., 7z x -o., Expand-Archive -DestinationPath logs); tar output files
    if prog in ARCHIVE_EXTRACT_PROGRAMS and _archive_extracts(prog, args):
        if any(_reaches_logs_dir(d or ".", cwd, base_dir) for d in _archive_dests(prog, args)):
            hits.extend(all_files)
    if prog in ("tar", "bsdtar"):
        for out_file in _tar_parse(args)["outputs"]:
            hits += _ground_truth_named(out_file) + _glob_ground_truth(out_file)
    if prog == "ln" and any(_is_logs_dir(a, cwd, base_dir) for a in operands + ([target_dir] if target_dir else [])):
        # A symlink/hard link to the logs dir is an alias that file tools would not recognise (ln -s logs st)
        hits.extend(all_files)
    if re.search(r"\bmklink\b", text, re.IGNORECASE) and any(_is_logs_dir(w, cwd, base_dir) for w in text.split()):
        hits.extend(all_files)
    if prog == "find":
        hits += _find_ground_truth(args, cwd, base_dir, depth)
    if prog == "git" and _git_wipes_logs(args):
        hits.extend(all_files)
    return _protected_hits(hits)


def _heredoc_program(head: str) -> str:
    """Program of the last sub-command of a heredoc operator's head (the command text before it on its line)."""
    subs = split_subcommands(head)
    return re.sub(r"\.exe$", "", _program(subs[-1])) if subs else ""


def _heredoc_feeds_interpreter(head: str, tail: str) -> bool:
    """True when the heredoc operator belongs to a python/node sub-command (python3 - <<EOF, node <<EOF) or to
    `cat <<EOF | python3` (head / tail: the operator line's command text before the operator / after its word);
    any other program (perl, bash, cat...) keeps its body in the line-by-line check."""
    prog = _heredoc_program(head)
    if CODE_INTERPRETER_PROGRAM_RE.match(prog):
        return True
    return prog == "cat" and bool(PIPE_TO_CODE_INTERPRETER_RE.match(tail))


def _heredoc_feeds_shell(head: str, tail: str) -> bool:
    """True when a shell (or eval / source / wsl / xargs / su / script) reads the heredoc: bash <<EOF, sh -s <<EOF,
    cat <<EOF | sh. A scan failure of such a body denies; any other body is data for its program."""
    if _heredoc_program(head) in SHELL_INTERPRETERS | HEREDOC_SHELL_READERS:
        return True
    return bool(re.search(r"\|&?\s*(?:\S*/)?(?:%s)(?:\.exe)?\b" % "|".join(
        sorted(SHELL_INTERPRETERS | HEREDOC_SHELL_READERS - {"."}, key=len, reverse=True)), tail))


def _heredoc_is_message(head: str, tail: str, outer: str) -> bool:
    """A heredoc inside "$(...)" read by a plain `cat` (no redirect, pipe or background) whose string is an argument
    of git / gh (outer: that command line up to the heredoc): `git commit -m "$(cat <<'EOF' ... EOF)"`. Its body is
    message text inside the quoted argument, not judged as shell lines; any other consumer (eval "$(cat <<EOF ...)",
    bash -c, echo ...) gets the body judged."""
    return (_heredoc_program(head) == "cat" and not re.search(r"[|<>&;]", head.split("cat", 1)[-1] + tail)
            and _heredoc_program(outer) in HEREDOC_MESSAGE_PROGRAMS)


def _inline_logs_dir_writes(command_line: str, cwd: str = "", base_dir: str = "") -> List[str]:
    """Protected files inline code can destroy through the logs/ directory: a 'logs' string literal (or a logs/ glob
    reaching a protected file) next to a destructive call (shutil.rmtree('logs'), fs.rmSync('logs'), os.remove over
    glob('logs/*')), or an ancestor / glob literal ('.', '..', the repo root, '*') next to a recursive delete or move."""
    broad = bool(INLINE_LOGS_DIR_MARKERS_RE.search(command_line) or INLINE_WRITE_MARKERS_RE.search(command_line))
    strong = bool(INLINE_ANCESTOR_MARKERS_RE.search(command_line))
    if not broad:
        return []
    hits: List[str] = []
    for m in INLINE_STRING_LITERAL_RE.finditer(command_line):
        literal = m.group(2)
        if not SHELL_GLOB_RE.search(literal) and _is_logs_dir(literal, cwd, base_dir):
            hits.extend(GROUND_TRUTH_FILES)
        hits += _glob_ground_truth(literal)
        if strong and _reaches_logs_dir(literal, cwd, base_dir):
            hits.extend(GROUND_TRUTH_FILES)
    return _protected_hits(hits)


def _lift_path_substitutions(command_line: str, runtime_prefix: bool = False) -> str:
    """Rewrites working-directory substitutions ($(pwd), `pwd`, $PWD, $(git rev-parse --show-toplevel)) to '.' and
    $HOME to '~', replaces a command substitution used as a path prefix ($(cmd)/logs) by '.' (runtime_prefix: by
    the run-time value $__subst__, whose `$__subst__/logs` _is_logs_dir treats as logs/), and any other one-line
    command substitution ($(cmd), `cmd`) by $__subst__; each lifted command is appended as a separate line. Unquoted, the tokenizer would split `$(pwd)/logs` into '$', '(', 'pwd', ')', '/logs' and the path
    would never reach the rm / ln operand checks."""
    line, lifted = _lift_substitutions(command_line, runtime_prefix)
    return line + "".join("\n" + c for c in lifted)


def _lift_substitutions(command_line: str, runtime_prefix: bool = False) -> Tuple[str, List[str]]:
    """(rewritten line, lifted commands) of _lift_path_substitutions. Applied to the raw text, so a substitution
    in a comment or a heredoc body (bash expands it in an unquoted body) is lifted too: the strict direction."""
    line = HOME_VAR_RE.sub("~", CWD_SUBSTITUTION_RE.sub(".", command_line))
    lifted: List[str] = []

    def lift(m: "re.Match", replacement: str) -> str:
        inner = m.group(1) if m.group(1) is not None else m.group(2)
        if HEREDOC_OPERATOR_RE.search(inner):
            # $(cat <<EOF): its body follows on the next lines, so the operator stays in place for _scan_shell
            # (removing it would leave the body as command text whose quotes pair with later ones)
            if inner not in lifted:
                lifted.append(inner)
            return m.group(0)
        if inner.strip():
            lifted.append(inner)
        return replacement

    line = PATH_SUBSTITUTION_RE.sub(lambda m: lift(m, "$__subst__" if runtime_prefix else "."), line)
    for _ in range(NESTED_DEPTH_LIMIT):  # innermost first: $(echo $(date))
        lifted_line = COMMAND_SUBSTITUTION_RE.sub(lambda m: lift(m, "$__subst__"), line)
        if lifted_line == line:
            break
        line = lifted_line
    return line, lifted


def _ground_truth_segments(command_line: str) -> List[Tuple[List[str], bool]]:
    """(sub-command tokens, fed by a pipe from the previous sub-command) for the ground-truth check: substitutions
    lifted (each lifted command tokenized on its own, so neither an open quote nor an unterminated heredoc body can
    swallow it), interpreter heredoc bodies dropped and `find` predicates split off by escaped parentheses / `\\;`
    re-attached to their find (find . \\( -type f \\) -delete)."""
    segments: List[Tuple[List[str], bool]] = []
    current: List[str] = []
    piped = False
    line, lifted = _lift_substitutions(command_line, runtime_prefix=True)
    tokens = _tokenize(line, drop_interpreter_bodies=True)
    for inner in lifted:
        tokens += ["\n"] + _tokenize(inner, drop_interpreter_bodies=True)
    for tok in tokens:
        if not tok:
            continue
        if tok in SHELL_SEPARATORS:
            if current:
                segments.append((current, piped))
                current = []
            if tok in ("|", "|&"):
                piped = True
            elif tok != "(":
                piped = False  # `a | (b; c)`: the subshell still reads the pipe
            continue
        current.append(_restore_quoted_newline(tok))
    if current:
        segments.append((current, piped))
    merged: List[Tuple[List[str], bool]] = []
    for tokens, piped in segments:
        if merged and _program(merged[-1][0]) == "find" and (tokens[0].startswith("-") or tokens[0] in ("!", ",")):
            merged[-1] = (merged[-1][0] + tokens, merged[-1][1])
        else:
            merged.append((list(tokens), piped))
    return merged


def _ground_truth_subcommands(command_line: str) -> List[List[str]]:
    """Sub-commands for the ground-truth check (see _ground_truth_segments)."""
    return [tokens for tokens, _ in _ground_truth_segments(command_line)]


_VAR_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _is_destructive_write(prog: str, args: List[str]) -> bool:
    """True for programs that create / overwrite / delete / move a path given as an operand."""
    if (prog in WRITE_PROGRAMS or prog in WINDOWS_DELETE_PROGRAMS or prog in WINDOWS_MOVE_PROGRAMS
            or prog in WINDOWS_COPY_PROGRAMS or prog == "rmdir"):
        return True
    if prog in ("sed", "perl") and any(re.match(r"^-[A-Za-z]*i", a) or a.startswith("--in-place") for a in args):
        return True
    return False


def _substitute_vars(token: str, var_map: Dict[str, str]) -> str:
    """Replace $VAR / ${VAR} by a known literal value (literal assignments earlier on the line)."""
    return _VAR_REF_RE.sub(lambda m: var_map.get(m.group(1) or m.group(2), m.group(0)), token)


def _update_var_map(tokens: List[str], var_map: Dict[str, str]) -> None:
    """Tracks literal shell variables across a line: a standalone `D=logs` (or D+=x), `export / declare / local
    D=logs` record a literal; a run-time value (D=$(...), read D, for D in ..., printf -v D, mapfile D), a prefix
    assignment that only lasts for its command (D=x cmd) and `unset D` forget it, so a later $D stays unresolved."""
    idx, info = _command_start(tokens)
    prog = os.path.basename(tokens[idx]).lower() if idx < len(tokens) else ""
    args = tokens[idx + 1:] if idx < len(tokens) else []
    for var, value in info["assigns"]:
        value = _substitute_vars(value, var_map)
        if idx >= len(tokens) and not _unresolved(value):
            var_map[var] = value
        else:
            var_map.pop(var, None)
    if prog == "unset":
        for a in args:
            var_map.pop(a, None)
        return
    for var, value in _declared_assignments(prog, args):
        if value is None:
            continue  # export D: keeps its value
        value = _substitute_vars(value, var_map)
        if prog in DECLARE_BUILTINS and not _unresolved(value):
            var_map[var] = value
        else:
            var_map.pop(var, None)


def _substituted_segments(command_line: str) -> List[Tuple[List[str], bool]]:
    """_ground_truth_segments with literal variables substituted in order (D=logs; rm -rf $D -> rm -rf logs)."""
    var_map: Dict[str, str] = {}
    out: List[Tuple[List[str], bool]] = []
    for tokens, piped in _ground_truth_segments(command_line):
        substituted = [_substitute_vars(t, var_map) for t in tokens]
        _update_var_map(substituted, var_map)
        out.append((substituted, piped))
    return out


def _join_cwd(base: str, rel: str) -> str:
    """rel resolved against the directory base (absolute POSIX or Windows rel is returned as is)."""
    raw = (rel or "").replace("\\", "/")
    if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw):
        return raw
    b = (base or "").replace("\\", "/")
    return posixpath.normpath(posixpath.join(b, raw)) if b else posixpath.normpath(raw or ".")


def _cwd_in_logs(cur_cwd: str, base_dir: str) -> bool:
    """True when a working directory is the workspace's logs/ or below it, or (fail safe, see _is_logs_dir) a
    directory named logs or below one anywhere (cd /proc/self/cwd/logs, cd /tmp/r/logs, env -C build/logs): a
    `logs` component of the path relative to the workspace root, or of the whole path when it lies outside it."""
    if not base_dir or not cur_cwd:
        return False
    raw = cur_cwd.replace("\\", "/")
    if _windows_form(raw) and "logs" in [c.lower() for c in raw.split("/")]:
        return True
    root = _canon_path(base_dir).rstrip("/")
    c = _canon_path(cur_cwd, base_dir)
    rel = c[len(root):] if c == root or c.startswith(root + "/") else c
    return "logs" in rel.split("/")


def _apply_cd(tokens: List[str], cwds: List[str], unknown: bool) -> Tuple[List[str], bool]:
    """Tracks `cd` / `pushd` across a line as a union: the new directory is ADDED to the possible working
    directories (cd may fail, run in a subshell or after `false &&`), so tracking only ever adds denials. `cd`
    with no operand, `cd -`, `cd ~/x`, `cd "$X"`, `cd $(...)`, `pushd +1` make the working directory unknown."""
    idx = _program_index(tokens)
    args = [a for a in _plain_args(tokens[idx + 1:]) if a not in ("-L", "-P", "-e", "-@", "-n", "--")]
    target = args[0] if args else None
    if target is None or target == "-" or target.startswith(("~", "+", "-")) or _unresolved(target):
        return cwds, True
    new = [_join_cwd(c, target) for c in cwds]
    merged = cwds + [c for c in new if c not in cwds]
    if len(merged) > 16:
        return cwds, True
    return merged, unknown or _proc_alias(target)  # cd /proc/self/cwd: resolves in the command's process


def _written_basenames(subs: List[List[str]], depth: int = 0) -> set:
    """Lower-case basenames of files a command line may write ('*' = names we cannot see): redirect targets and
    time -o files; the write targets of writers (tee f, cp x f, dd of=f, sed -i f), output options (curl -o f,
    --output=f); every word of inline interpreter code (python3 -c "open('f','w')") and of git sub-commands that
    rewrite the work tree (git checkout f); '*' for archive extraction and recursive / glob copies; the same for the
    nested command lines of each sub-command (bash -c '...')."""
    out: set = set()
    for tokens in subs:
        idx, info = _command_start(tokens)
        words: List[str] = list(_redirect_targets(tokens) + info["outputs"])
        prog_token = tokens[idx] if idx < len(tokens) else ""
        prog = re.sub(r"\.exe$", "", os.path.basename(prog_token).lower())
        args = _plain_args(tokens[idx + 1:] if idx < len(tokens) else [])
        if _is_destructive_write(prog, args) and prog not in CONTENT_PRESERVING_WRITERS:
            words += _write_targets(prog, args)
            if prog in LOGS_DIR_COPY_PROGRAMS | WINDOWS_COPY_PROGRAMS and (
                    _is_recursive(args) or any(SHELL_GLOB_RE.search(a) for a in args)):
                out.add("*")
        options = OUTPUT_OPTIONS.get(prog, set())
        for j, a in enumerate(args):
            m = OUTPUT_VALUE_RE.match(a)
            if m:
                words.append(a[m.end():])
            elif a in options and j + 1 < len(args):
                words.append(args[j + 1])
        if prog in ARCHIVE_EXTRACT_PROGRAMS and _archive_extracts(prog, args):
            out.add("*")
        if SHELL_CALL_INTERPRETERS_RE.match(prog) or (prog == "git" and _git_subcommand(args)[0] in
                                                       GIT_WRITE_SUBCOMMANDS):
            words += args
        if depth < NESTED_DEPTH_LIMIT:  # bash -c 'echo ls > /tmp/a.sh'; bash /tmp/a.sh
            for nested in _nested_commands(prog, args) + info["nested"]:
                out |= _written_basenames(_ground_truth_subcommands(nested), depth + 1)
        for t in words:
            for w in re.findall(r"[^\s'\"();,=<>|&`{}\[\]]+", t or ""):
                out.add(posixpath.basename(w.replace("\\", "/")).lower())
    out.discard("")
    return out


def _xargs_command_read_only(cmd: List[str]) -> bool:
    """An xargs command that only reads (default echo, cat, grep, wc, git log ...)."""
    if not cmd:
        return True
    prog = re.sub(r"\.exe$", "", os.path.basename(cmd[0]).lower())
    if _unresolved(cmd[0]):
        return False
    if prog in GROUND_TRUTH_READ_PROGRAMS or prog == "git":
        return _ground_truth_read_only(prog, cmd[1:])
    return prog in XARGS_READ_PROGRAMS


def _is_confined_find(stage: List[str], cwds: List[str], unknown: bool, base_dir: str) -> bool:
    """A producer stage that only lists paths provably outside logs/ and its ancestors: `find` over literal roots
    (no glob / variable / ~; relative ones only with a known cwd) whose path and realpath are neither logs/, below
    it nor an ancestor of it, without -L / -follow, and printing nothing but the paths (-print / -print0)."""
    idx, info = _command_start(stage)
    if idx >= len(stage) or os.path.basename(stage[idx]).lower() != "find" or info["wrappers"]:
        return False
    if any(t in ("<", "<<", "<<<") for t in stage):
        return False
    args = _plain_args(stage[idx + 1:])
    if any(a in FIND_NON_LISTING_ACTIONS for a in args):
        return False
    for root in _find_roots(args):
        if _unresolved(root) or SHELL_GLOB_RE.search(root) or root.startswith("~") or (unknown and _relative_word(root)):
            return False
        for c in cwds:
            try:
                real = os.path.realpath(_host_path(root, _host_path(c, base_dir) if c else base_dir))
            except (OSError, ValueError):
                return False
            for path, base in ((root, c), (real, "")):
                if (_reaches_logs_dir(path, base, base_dir) or _is_logs_dir(path, base, base_dir)
                        or _cwd_in_logs(_join_cwd(base, path) if base else path, base_dir)):
                    return False
    return True


def _xargs_unconfined(tokens: List[str], producers: List[List[str]], cwds: List[str], unknown: bool,
                      base_dir: str) -> bool:
    """`xargs` (also after wrappers: nice / timeout 5 / env / command / exec ... xargs) running anything but a
    read-only command is denied unless it reads its items from a pipe whose every producer is a confined find
    listing (_is_confined_find), and only deleters (rm / rmdir / unlink) or copiers with -t DIR (cp / mv / ln /
    install) run on them. No producer (xargs rm < list), -a FILE, -d DELIM, -I / -i replacement, a shell, an
    interpreter, env or another wrapper -> denied."""
    idx, info = _command_start(tokens)
    x = info["xargs"]
    if x is None:
        return False
    cmd = _plain_args(tokens[x["cmd_index"]:])
    if _xargs_command_read_only(cmd):
        return False
    if x["arg_file"] or x["delimiter"] or x["replace"] is not None or not producers:
        return True
    if any(t in ("<", "<<", "<<<", "<&") for t in tokens):
        return True
    prog = re.sub(r"\.exe$", "", os.path.basename(cmd[0]).lower())
    if prog not in XARGS_DELETE_PROGRAMS:
        if prog not in XARGS_TARGET_PROGRAMS or _split_operands(prog, cmd[1:])[1] is None:
            return True
    return not all(_is_confined_find(p, cwds, unknown, base_dir) for p in producers)


def _stdin_shell_hits(tokens: List[str], producers: List[List[str]], cwds: List[str], unknown: bool, base_dir: str,
                      depth: int, written: frozenset) -> List[str]:
    """A shell reading its commands from stdin (`... | bash`, `bash -s`, `sh < f`, `source /dev/stdin`,
    `bash <(curl ...)`) is denied unless its input is visible: a heredoc (its body is judged line by line), a
    here-string (judged), `< file` or a single `cat <file>` / `cat < file` producer (the file is judged)."""
    deny = list(GROUND_TRUTH_FILES)
    idx, info = _command_start(tokens)
    if idx >= len(tokens):
        return []
    prog = re.sub(r"\.exe$", "", os.path.basename(tokens[idx]).lower())
    raw_args = tokens[idx + 1:]
    args = _plain_args(raw_args)
    if prog in SHELL_INTERPRETERS:
        parsed = _shell_args(args)
        if parsed["info"] or parsed["command"] is not None:
            return []
        if not (parsed["stdin"] or parsed["script"] in STDIN_SCRIPT_PATHS):
            return []
    elif prog in (".", "source"):
        operand = next((a for a in args if not a.startswith("-")), None)
        if operand is not None and operand not in STDIN_SCRIPT_PATHS:
            return []
    else:
        return []

    def judge(word: str) -> List[str]:
        out: List[str] = []
        for c in cwds:
            out += _judge_script(word, c, base_dir, depth, unknown, True, written)
        return out

    for j, t in enumerate(raw_args):
        if t in ("<<", "<<-"):
            return []  # heredoc: its body is judged as shell lines right after this one (_heredoc_body_tokens)
        if t == "<<<":
            if j + 1 >= len(raw_args):
                return deny
            out: List[str] = []
            for c in cwds:
                out += _ground_truth_line_hits(raw_args[j + 1], c, base_dir, depth=depth + 1, cwd_unknown=unknown,
                                               written=written)
            return out
        if t in ("<", "<&"):
            target = raw_args[j + 1] if j + 1 < len(raw_args) else ""
            if not target or target.isdigit() or target in SHELL_SEPARATORS:
                return deny
            return judge(target)
    if len(producers) == 1:
        p_idx, p_info = _command_start(producers[0])
        rest = producers[0][p_idx + 1:]
        if p_idx < len(producers[0]) and os.path.basename(producers[0][p_idx]).lower() == "cat" and \
                not p_info["wrappers"]:
            if len(rest) == 2 and rest[0] == "<":
                return judge(rest[1])
            if len(rest) == 1 and not rest[0].startswith("-"):
                return judge(rest[0])
    return deny


def _ground_truth_line_hits(command_line: str, cwd: str, base_dir: str, shell: str = "bash", depth: int = 0,
                            cwd_unknown: bool = False, strict: bool = True, written: frozenset = frozenset(),
                            cwds: Optional[List[str]] = None) -> List[str]:
    """Judge a whole command line: literal variables substituted in order, `cd` / `pushd` tracked as a union of
    possible working directories (every sub-command is judged against each), xargs pipelines and shells reading
    stdin checked against their producers, and the basenames written anywhere on the line (and the enclosing lines)
    collected so that running a script written on the same line is denied. Nested command lines and script files
    re-enter here one level deeper; beyond NESTED_DEPTH_LIMIT the line is denied. shell="powershell": per
    sub-command only (no Bash tracking). strict=False (only the sha256-pinned DESK_SHELL_SCRIPTS): the run-time-value
    and unknown-cwd rules are skipped; xargs, stdin-shell and every literal rule still apply. cwds: the possible
    starting directories (default [cwd]). Within one audit scope the result is memoised per (text, cwds, flags)."""
    if depth > NESTED_DEPTH_LIMIT:
        return list(GROUND_TRUTH_FILES)
    cwd = cwd or base_dir
    start_cwds = list(dict.fromkeys(cwds)) if cwds else [cwd]
    key = ("line", command_line, tuple(start_cwds), base_dir, shell, depth, cwd_unknown, strict, written)
    memo = _AUDIT["memo"] if _AUDIT["active"] else None
    if memo is not None and key in memo:
        return memo[key]
    result = _ground_truth_line_hits_uncached(command_line, start_cwds, base_dir, shell, depth, cwd_unknown, strict,
                                              written)
    if memo is not None:
        memo[key] = result
    return result


def _ground_truth_line_hits_uncached(command_line: str, start_cwds: List[str], base_dir: str, shell: str, depth: int,
                                     cwd_unknown: bool, strict: bool, written: frozenset) -> List[str]:
    cwd = start_cwds[0]
    if shell != "bash":
        for tokens in _ground_truth_subcommands(command_line):
            protected = _ground_truth_writes(tokens, " ".join(tokens), cwd, base_dir, depth, shell=shell)
            if protected:
                return protected
        return []
    segments = _substituted_segments(command_line)
    written = frozenset(set(written) | _written_basenames([tokens for tokens, _ in segments]))
    cwds, unknown = list(start_cwds), cwd_unknown
    start = 0
    for k, (tokens, piped) in enumerate(segments):
        if not piped:
            start = k
        hits: List[str] = []
        producers = [t for t, _ in segments[start:k]]
        if _xargs_unconfined(tokens, producers, cwds, unknown, base_dir):
            return list(GROUND_TRUTH_FILES)
        hits += _stdin_shell_hits(tokens, producers, cwds, unknown, base_dir, depth, written)
        text = " ".join(tokens)
        for c in cwds:
            hits += _ground_truth_writes(tokens, text, c, base_dir, depth, cwd_unknown=unknown, strict=strict,
                                         written=written, cwd_set=tuple(cwds))
        if hits:
            return _protected_hits(hits)
        prog = _program(tokens)
        if prog in ("cd", "pushd"):
            cwds, unknown = _apply_cd(tokens, cwds, unknown)
        elif prog == "popd":
            unknown = True
    return []


def _resolve_script_path(token: str, cwd: str, base_dir: str) -> Optional[str]:
    if not token.endswith(".py"):
        return None
    path = os.path.expanduser(token)
    if not os.path.isabs(path):
        path = os.path.join(cwd or base_dir, path)
    return os.path.normpath(path)


def _unsanctioned_trading_script(tokens: List[str], cwd: str, base_dir: str) -> Optional[str]:
    """Returns the path of an executed .py file outside scripts/ and tests/ that contains trading primitives."""
    idx = _program_index(tokens)
    if idx >= len(tokens):
        return None
    prog = os.path.basename(tokens[idx]).lower()
    candidates = []
    if prog.endswith(".py"):
        candidates.append(tokens[idx])
    elif prog.startswith("python"):
        for tok in tokens[idx + 1:]:
            if tok.startswith("-"):
                continue
            candidates.append(tok)
            break
    base_norm = os.path.normcase(os.path.normpath(base_dir))
    for tok in candidates:
        path = _resolve_script_path(tok, cwd, base_dir)
        if not path or not os.path.isfile(path):
            continue
        rel = os.path.relpath(os.path.normcase(path), base_norm).replace("\\", "/")
        if not rel.startswith("..") and (rel.startswith("scripts/") or rel.startswith("tests/")):
            continue
        wt_rel = _linked_worktree_rel(path, base_dir)
        if wt_rel and (wt_rel.startswith("scripts/") or wt_rel.startswith("tests/")):
            continue
        try:
            if os.path.getsize(path) > 2 * 1024 * 1024:
                return path
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                if SCRIPT_TRADING_PRIMITIVES_RE.search(f.read()):
                    return path
        except OSError:
            continue
    return None


# -----------------------------------------------------------------------------
# PowerShell tool (Claude Code on Windows)
# -----------------------------------------------------------------------------
def _ps_scan(text: str, i: int, closer: Optional[str], depth: int) -> Tuple[str, str, List[str], int]:
    """Quote-, escape- and comment-aware pass over PowerShell text from index i up to `closer` (or the end).
    Returns (outer, full, bodies, end): outer has every nested body replaced by PS_NESTED_PLACEHOLDER, full has them
    inlined as ' ( body ) ' (closing / reopening the string for "...$(...)..."), bodies are the raw texts of the
    direct {...}, $(...), @(...), @{...} and (...) bodies, end is the index after `closer`.
    '...' and @'...'@ are literal ('' is a quote); in "...", @"..."@ and bare text a backtick escapes the next
    character (backtick + newline = continuation) and only $( opens a body inside strings. Comments outside strings
    (# at a token start to the end of the line, <# ... #>) are dropped. Quote characters inside strings are dropped
    so the POSIX tokenizer sees balanced quoting. Raises ValueError on unbalanced quotes, brackets or block comments."""
    if depth > PS_SCAN_DEPTH_LIMIT:
        raise ValueError("brackets nested too deeply")
    outer: List[str] = []
    full: List[str] = []
    bodies: List[str] = []
    n = len(text)
    mode = ""  # "" bare text, "'" / '"' strings, "@'" / '@"' here-strings

    def emit(s: str) -> None:
        outer.append(s)
        full.append(s)

    def nested(start: int, close: str, in_string: bool) -> int:
        _outer, inner_full, _bodies, end = _ps_scan(text, start, close, depth + 1)
        bodies.append(text[start:end - 1])
        outer.append(PS_NESTED_PLACEHOLDER)
        full.append(('" ( ' + inner_full + ' ) "') if in_string else (" ( " + inner_full + " ) "))
        return end

    while i < n:
        c = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if mode in ("'", "@'"):
            if mode == "'" and c == "'":
                if nxt == "'":
                    i += 2  # '' is a literal quote
                    continue
                emit("'")
                mode = ""
            elif mode == "@'" and c == "\n" and text.startswith("'@", i + 1):
                emit("\n'")
                mode = ""
                i += 2
            elif c != "'":
                emit(c)
            i += 1
            continue
        if c == "`":
            if text.startswith("\r\n", i + 1):
                emit(" ")
                i += 3
                continue
            emit(" " if nxt == "\n" else ("" if nxt in ("'", '"') else nxt))
            i += 2
            continue
        if mode in ('"', '@"'):
            if mode == '"' and c == '"':
                if nxt == '"':
                    i += 2  # "" is a literal quote
                    continue
                emit('"')
                mode = ""
            elif mode == '@"' and c == "\n" and text.startswith('"@', i + 1):
                emit('\n"')
                mode = ""
                i += 2
            elif c == "$" and nxt == "(":
                i = nested(i + 2, ")", True)
                continue
            elif c != '"':
                emit(c)
            i += 1
            continue
        boundary = i == 0 or text[i - 1] in PS_TOKEN_BOUNDARY
        if c == "@" and nxt in ("'", '"') and re.match(r"[ \t]*\r?\n", text[i + 2:]):
            emit(nxt)
            mode = "@" + nxt
            i = text.index("\n", i + 2) + 1
        elif c in ("'", '"'):
            emit(c)
            mode = c
            i += 1
        elif c == "#" and boundary:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c == "<" and nxt == "#" and boundary:
            j = text.find("#>", i + 2)
            if j < 0:
                raise ValueError("unterminated <# block comment")
            emit(" ")
            i = j + 2
        elif c == "$" and nxt == "{":
            j = text.find("}", i + 2)
            if j < 0:
                raise ValueError("unterminated ${...} variable")
            emit(text[i:j + 1])
            i = j + 1
        elif c in "$@" and nxt == "(":
            i = nested(i + 2, ")", False)
        elif c == "@" and nxt == "{":
            i = nested(i + 2, "}", False)
        elif c in PS_OPENERS:
            i = nested(i + 1, PS_OPENERS[c], False)
        elif c in ")}":
            if c != closer:
                raise ValueError(f"unbalanced '{c}'")
            return "".join(outer), "".join(full), bodies, i + 1
        else:
            emit(c)
            i += 1
    if mode:
        raise ValueError("unterminated string")
    if closer:
        raise ValueError(f"missing '{closer}'")
    return "".join(outer), "".join(full), bodies, i


def scan_powershell(command_line: str) -> Tuple[str, str, List[str]]:
    """(outer, full, bodies) of a PowerShell command prepared for the shell analysis (never executed): Unicode
    quotes and dashes mapped to ASCII, CRLF and lone CR turned into LF (PowerShell line ends), _ps_scan applied and
    backslashes turned into '/' (PowerShell does not escape with backslash, so C:\\repo\\logs must not lose its
    separators in the POSIX tokenizer). Raises ValueError."""
    # PowerShell ends lines (comments, statements) at a lone \r too
    text = (command_line or "").translate(PS_UNICODE_TRANSLATION).replace("\r\n", "\n").replace("\r", "\n")
    outer, full, bodies, _ = _ps_scan(text, 0, None, 0)
    return outer.replace("\\", "/"), full.replace("\\", "/"), bodies


def normalize_powershell_command(command_line: str) -> str:
    """Whole PowerShell command (nested bodies inlined) as analysed text; raises ValueError (see scan_powershell)."""
    return scan_powershell(command_line)[1]


def _powershell_tokens(command_line: str) -> List[str]:
    """Quote-aware tokens of a normalised PowerShell command. Unlike _tokenize there is no fallback: unbalanced
    quotes raise, so the PowerShell path fails closed."""
    lexer = shlex.shlex(command_line, posix=True, punctuation_chars=SHELL_PUNCTUATION)
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    lexer.commenters = ""
    return [part for tok in lexer for part in _split_operators(tok)]


def powershell_payload_denial(command_line: str) -> Optional[str]:
    """Denial for PowerShell payloads judged on the whole command (any target): unauditable encoded payloads
    (powershell/pwsh -EncodedCommand / -enc / -ec / -e, FromBase64String next to iex / Invoke-Expression /
    Invoke-Command / & / powershell) and raw HTTP writes to Binance through Invoke-RestMethod / Invoke-WebRequest."""
    if PS_ENCODED_COMMAND_RE.search(command_line) or (
            PS_FROM_BASE64_RE.search(command_line) and PS_BASE64_RUNNER_RE.search(command_line)):
        return (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Obfuscated Execution): PowerShell encoded payloads (powershell/pwsh "
            "-EncodedCommand, FromBase64String run through iex / & / powershell) cannot be audited and are forbidden. "
            "Run the plain command instead."
        )
    if PS_HTTP_CMDLET_RE.search(command_line) and BINANCE_HOST_RE.search(command_line) and \
            PS_HTTP_WRITE_RE.search(command_line):
        return (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Raw HTTP write requests to Binance "
            f"(fapi / MCP gateway) are strictly forbidden. Route orders through {CHOKE_POINT}."
        )
    return None


def _powershell_words(tokens: List[str]) -> List[str]:
    """Path-like values of PowerShell tokens: the token, option values (-Path:logs, --x=y) and array items (a,logs)."""
    words: List[str] = []
    for i, tok in enumerate(tokens):
        if tok in SHELL_SEPARATORS or _is_redirect(tok):
            continue
        if tok == "*" and i + 1 < len(tokens) and _is_redirect(tokens[i + 1]):
            continue  # *> redirects every stream, it is not a glob
        for value in {tok, _word_value(tok), PS_NAMED_VALUE_RE.sub("", tok)}:
            words.extend(w for w in value.split(",") if w)
    return words


def _powershell_redirects(tokens: List[str]) -> bool:
    """True when the command redirects a stream anywhere but $null (>, >>, 2>, *>, n>&m)."""
    return any(_is_redirect(tok) and (tokens[i + 1] if i + 1 < len(tokens) else "").lower() != "$null"
               for i, tok in enumerate(tokens))


def _powershell_write_construct(command_line: str, tokens: List[str]) -> Optional[str]:
    """First construct that can write a file or run code (write/exec cmdlets anywhere, .NET static or method calls,
    provider variables, call operator / dot-sourcing, redirection); None when the command can only read."""
    for label, rx in (("write cmdlet", PS_WRITE_CMDLET_RE), ("code-running cmdlet", PS_EXEC_CMDLET_RE),
                      (".NET static call", PS_DOTNET_STATIC_RE), ("method call", PS_METHOD_CALL_RE),
                      ("provider variable", PS_PROVIDER_VARIABLE_RE),
                      ("call operator / dot-sourcing", PS_CALL_OPERATOR_RE)):
        m = rx.search(command_line)
        if m:
            return f"{label} '{m.group(0).strip()}'"
    return "output redirection" if _powershell_redirects(tokens) else None


def _wsl_invocation(args: List[str]) -> Optional[Tuple[List[str], bool]]:
    """(Linux command, through the default shell) run by `wsl.exe [-d X] [-u X] [--cd X] [--|-e] cmd...` ([] when
    none). Without -e/--exec wsl joins the arguments and its default shell parses them again: both the Bash and the
    PowerShell paths judge that joined line once more (_wsl_shell_line). None for any other wsl option (--import,
    --mount, --export...), which is judged like an unknown program."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in WSL_COMMAND_OPTIONS:
            return args[i + 1:], a == "--"
        if a in WSL_VALUE_OPTIONS:
            i += 2
        elif "=" in a and a.partition("=")[0] in WSL_VALUE_OPTIONS:
            i += 1
        elif a.startswith("-"):
            return None
        else:
            return args[i:], True
    return [], True


def _wsl_command(args: List[str]) -> Optional[List[str]]:
    """Linux command tokens of a wsl.exe invocation (see _wsl_invocation)."""
    invocation = _wsl_invocation(args)
    return invocation[0] if invocation is not None else None


def _wsl_leading_options(args: List[str]) -> List[Tuple[str, str]]:
    """(option, value) pairs of the value-taking wsl.exe options before the Linux command (-d X, -u X, --cd X ...)."""
    out: List[Tuple[str, str]] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a in WSL_VALUE_OPTIONS:
            out.append((a, args[i + 1] if i + 1 < len(args) else ""))
            i += 2
        elif "=" in a and a.partition("=")[0] in WSL_VALUE_OPTIONS:
            opt, _, val = a.partition("=")
            out.append((opt, val))
            i += 1
        else:
            break
    return out


def _wsl_cd(args: List[str]) -> Optional[str]:
    """Value of the last `--cd DIR` among the wsl.exe options (before the Linux command); None without one."""
    cd = None
    for opt, value in _wsl_leading_options(args):
        if opt == "--cd" and value:
            cd = value
    return cd


def _wsl_shell_line(seg: List[str], idx: int) -> Optional[str]:
    """Command line that the wsl.exe call at seg[idx] hands to the Linux default shell (no -e/--exec), None when it
    is not one: the arguments joined with spaces (the conservative model of what the shell parses again, wsl --
    cat x '>logs/session_state.json' redirects), after `cd DIR &&` for --cd DIR so the cwd is tracked. Shared by
    the Bash (_bash_wsl_shell_commands) and PowerShell (_powershell_wsl_shell_commands) paths."""
    if idx >= len(seg) or re.sub(r"\.exe$", "", os.path.basename(seg[idx]).lower()) != "wsl":
        return None
    args = _plain_args(seg[idx + 1:])
    invocation = _wsl_invocation(args)
    if invocation is None or not invocation[1] or not invocation[0]:
        return None
    line = " ".join(invocation[0])
    cd = _wsl_cd(args)
    return f"cd {shlex.quote(cd)} && {line}" if cd is not None else line


def _bash_wsl_shell_commands(command_line: str) -> List[str]:
    """Command lines that the wsl.exe calls of a Bash line hand to the Linux default shell (see _wsl_shell_line):
    Git Bash passes an argument without spaces bare (--symbol 'X;touch${IFS}y'), and the Linux shell runs it."""
    lines: List[str] = []
    for seg in split_subcommands(command_line):
        line = _wsl_shell_line(seg, _program_index(seg))
        if line is not None:
            lines.append(line)
    return lines


def _powershell_wsl_shell_commands(command_line: str) -> List[str]:
    """Command lines that wsl.exe hands to the Linux default shell (no -e/--exec), see _wsl_shell_line."""
    lines: List[str] = []
    segments: List[List[str]] = [[]]
    for tok in _powershell_tokens(command_line):
        if tok in SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    for seg in segments:
        line = _wsl_shell_line(seg, 0)
        if line is not None:
            lines.append(line)
    return lines


def _powershell_unlisted_command(tokens: List[str]) -> Optional[str]:
    """Leading command of the first statement / pipeline segment outside PS_READ_CMDLETS. Expressions are not
    commands ($_.Length -gt 0, `(Get-Content x | ConvertFrom-Json).is_valid`), assignments are judged by their
    value ($x = robocopy ...), and wsl.exe <linux command> is left to the Bash ground-truth analysis."""
    segments: List[List[str]] = [[]]
    for tok in tokens:
        if tok in SHELL_SEPARATORS:
            segments.append([])
        else:
            segments[-1].append(tok)
    for seg in segments:
        while len(seg) > 1 and PS_EXPRESSION_LEAD_RE.match(seg[0]) and seg[1].startswith(PS_ASSIGNMENT_OPERATORS):
            op = next(o for o in PS_ASSIGNMENT_OPERATORS if seg[1].startswith(o))
            seg = ([seg[1][len(op):]] if seg[1][len(op):] else []) + seg[2:]
        if not seg or seg[0].lower() in PS_READ_CMDLETS or PS_EXPRESSION_LEAD_RE.match(seg[0]):
            continue
        if re.sub(r"\.exe$", "", os.path.basename(seg[0]).lower()) == "wsl" and _wsl_command(seg[1:]) is not None:
            continue
        return seg[0]
    return None


def _powershell_statement_program(tokens: List[str], k: int) -> bool:
    """True when tokens[k] is the program of its statement: first token, after a separator / the & call operator,
    or the command of a `wsl.exe [options] --|-e|--exec <cmd>` statement."""
    if k == 0 or tokens[k - 1] in SHELL_SEPARATORS:
        return True
    if tokens[k - 1] not in WSL_COMMAND_OPTIONS:
        return False
    s = k - 1
    while s > 0 and tokens[s - 1] not in SHELL_SEPARATORS:
        s -= 1
    return ntpath.basename(tokens[s]).lower() in ("wsl", "wsl.exe")


def _powershell_writes_gate_program(command_line: str, tokens: List[str]) -> bool:
    """True when a PowerShell command names a gate module that is also run as a program (GATE_PROGRAM_PATH_RE)
    anywhere other than as the script a Python interpreter runs, next to any write construct
    (_powershell_write_construct: write cmdlets, redirection, .NET / method calls, provider variables, call
    operator...). Parenthesized targets (-Path ('x'), (Join-Path . x)) and variables ($p = 'x'; Set-Content $p) are
    covered because any such mention counts. Running the program (python scripts\\execute_futures_trade.py
    --close-position 2>&1 | Out-File x.log, & python ..., wsl.exe -- python3 ...) is not a write."""
    if not GATE_PROGRAM_PATH_RE.search(command_line):
        return False
    named = False
    for i, tok in enumerate(tokens):
        if not GATE_PROGRAM_PATH_RE.search(tok):
            continue
        j = i - 1
        while j >= 0 and tokens[j].startswith("-") and tokens[j] not in SHELL_SEPARATORS:
            j -= 1  # interpreter options (python -u -B script.py)
        run = (GATE_PROGRAM_TOKEN_RE.fullmatch(tok) is not None and j >= 0
               and PS_PYTHON_RUNNER_RE.match(ntpath.basename(tokens[j]).lower()) is not None
               and _powershell_statement_program(tokens, j))
        if not run:
            named = True
            break
    return named and _powershell_write_construct(command_line, tokens) is not None


def powershell_backstop(command_line: str, cwd: str, base_dir: str) -> Tuple[Optional[str], Optional[str]]:
    """(deny reason, force_ask reason) for a normalised PowerShell command, applied on top of analyze_run_command.
    A command naming a ground-truth file (also through a logs/ glob or an 8.3 short name), the evaluation trail or
    the logs/ directory literally is denied unless every statement starts with a read-only or navigation cmdlet
    (PS_READ_CMDLETS) and nothing in it can write or run code; a glob that can expand to logs/ (*, log*) is denied
    next to a write construct; harness paths next to a write construct require confirmation.
    Raises on unbalanced quotes (the caller denies)."""
    tokens = _powershell_tokens(command_line)
    words = _powershell_words(tokens)
    ground_truth = _ground_truth_named(command_line)
    for w in words:
        ground_truth += _glob_ground_truth(w)
    trail = bool(EVALUATION_TRAIL_CMD_RE.search(command_line) or TRANSCRIPT_ROOT_OVERRIDE_RE.search(command_line))
    if any(PS_SHORT_NAME_RE.search(_shell_path(w)) for w in words):
        ground_truth += list(GROUND_TRUTH_FILES)
    # A literal logs/ word is judged like a protected file; a glob that can expand to logs/ (*, log*) only next to a
    # write construct (git add * stays usable)
    logs_words = [w for w in words if _is_logs_dir(w, cwd, base_dir)]
    logs_dir = bool(logs_words)
    logs_literal = any(not SHELL_GLOB_RE.search(w) for w in logs_words)
    harness = bool(HARNESS_PATH_CMD_RE.search(command_line))
    gate_program_write = _powershell_writes_gate_program(command_line, tokens)
    git_config = any(_git_exec_config_path(w) for w in words)
    if not (ground_truth or trail or logs_dir or harness or git_config or gate_program_write):
        return None, None
    construct = _powershell_write_construct(command_line, tokens)
    if ground_truth and not construct and not trail:
        # Issue #191: one statement running a read-only analysis script that names only its own ground-truth output
        # (its sole sanctioned writer, _names_only_own_ground_truth); the analysis decides the rest
        subs = [s for s in split_subcommands(command_line) if s]
        if len(subs) == 1 and _names_only_own_ground_truth(subs[0], ground_truth, _command_start(subs[0])[1], cwd,
                                                           base_dir):
            ground_truth = []
    unlisted = None if construct else _powershell_unlisted_command(tokens)
    how = construct or (f"command '{unlisted}' outside the read-only cmdlets" if unlisted else None)
    suffix = (f" PowerShell {how} next to a protected path: only read-only cmdlets (Get-Content, Select-String, "
              "Test-Path, Get-ChildItem...) may name it.")
    if trail and how:
        return ("🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Commands must not write "
                "logs/evaluations/, latest_dossier.json, Antigravity brain transcripts or Claude Code subagent "
                "transcripts." + suffix + " " + EVALUATOR_HINT), None
    if ground_truth and how:
        return ground_truth_denial(ground_truth) + suffix, None
    if (logs_literal and how) or (logs_dir and construct):
        return ground_truth_denial(list(GROUND_TRUTH_FILES)).rstrip(".") + " (they live in logs/)." + suffix, None
    if git_config and construct:
        return GIT_EXEC_CONFIG_REASON + f" PowerShell {construct} next to a git config / hook path.", None
    if (harness and construct) or gate_program_write:
        return None, "PowerShell " + HARNESS_WRITE_REASON[0].lower() + HARNESS_WRITE_REASON[1:]
    return None, None


def analyze_run_command(command_line: str, cwd: str, base_dir: str, shell: str = "bash") -> Dict[str, Any]:
    """
    Classifies a shell command (shell="powershell": a command already normalised by
    normalize_powershell_command, whose read-only cmdlets may name ground-truth files). Returns
    {deny: reason|None, force_ask: reason|None, trading: [subcommand text], trading_tokens: [its tokens],
     risk_reducing: bool, risk_blocker: why a risk-reducing sub-command may not be auto-allowed|None,
     all_safe: bool, record_eval: {...}|None}
    Runs inside the evaluation's audit scope; exceeding the work budget denies (fail closed).
    """
    with _audit_scope():
        try:
            return _analyze_run_command(command_line, cwd, base_dir, shell)
        except AuditBudgetExceeded:
            return {"deny": AUDIT_BUDGET_REASON, "force_ask": None, "trading": [], "batch": [],
                    "risk_reducing": False, "risk_blocker": None, "all_safe": False, "record_eval": None}


def _risk_auto_allow_blocker(tokens: List[str]) -> Optional[str]:
    """Why a risk-reducing sub-command may not be auto-allowed on its own: a token holding a shell metacharacter
    (RISK_AUTO_ALLOW_METACHARS: wsl.exe hands its arguments to a shell that parses them again; redirects write
    files), --move-breakeven --force (FORCED_BREAKEVEN_BLOCKER, issue #111) or a prefix the hook cannot vouch
    for (_risk_prefix_blocker). None when it may."""
    for tok in tokens:
        for ch in RISK_AUTO_ALLOW_METACHARS:
            if ch in tok:
                return f"an argument holds the shell metacharacter {ch!r}"
    toks, i, _ = _executed_script_at(tokens)
    names = {a.partition("=")[0] for a in _plain_args(toks[i + 1:])} if i >= 0 else set()
    if "--force" in names and names & EXECUTOR_MOVE_BREAKEVEN_FLAGS:
        return FORCED_BREAKEVEN_BLOCKER
    return _risk_prefix_blocker(tokens)


def _risk_prefix_blocker(tokens: List[str]) -> Optional[str]:
    """Walks every level _executed_script_at crosses (assignments, wrappers such as env / sudo / timeout, wsl.exe,
    python) and returns why the call is not auto-allowed: a directory change at any level (env -C, sudo -D,
    wsl.exe --cd, also inside wsl -e), an env assignment outside RISK_ENV_ASSIGNMENTS (PYTHONPATH=..., env
    LD_...=...), a wrapper outside RISK_WRAPPERS (sudo, doas, chroot, setsid, flock, time, uv run ...: issue #110),
    wsl.exe -u / --user or -d / --distribution other than WSL_DISTRO_NAME,
    a wrapper fed by stdin or a string (xargs, env -S), or a python option
    outside RISK_PYTHON_OPTIONS (-i, -m, -c, -W...). None when the prefix is clean."""
    for _ in range(NESTED_DEPTH_LIMIT + 2):
        idx, info = _command_start(tokens)
        if info["chdirs"]:
            return "it changes the working directory"
        if info["xargs"] or info["nested"] or info["outputs"]:
            return "it runs through a wrapper that takes arguments, a command string or an output file"
        for wrapper in info["wrappers"]:
            if wrapper not in RISK_WRAPPERS:
                return f"it runs through the wrapper {wrapper} (another user, root, shell or process context)"
        for var, _value in info["assigns"]:
            if var not in RISK_ENV_ASSIGNMENTS:
                return f"it sets the environment variable {var}"
        if idx >= len(tokens):
            return None
        prog = re.sub(r"\.exe$", "", os.path.basename(tokens[idx]).lower())
        if prog == "wsl":
            if _wsl_cd(tokens[idx + 1:]) is not None:
                return "it changes the working directory"
            own = os.environ.get("WSL_DISTRO_NAME", "").lower()
            pairs = _wsl_leading_options(tokens[idx + 1:])
            for opt, value in pairs:
                if opt == "--shell-type" and value.lower() == "login":
                    return "it runs a wsl.exe login shell (--shell-type login sources the profile)"
                if opt in ("-u", "--user"):
                    return "it runs wsl.exe as another user (-u / --user)"
                if opt in ("-d", "--distribution") and (not own or value.lower() != own):
                    return "it runs in another WSL distribution (-d / --distribution other than WSL_DISTRO_NAME)"
            linux = _wsl_command(tokens[idx + 1:])
            if not linux:
                return None
            tokens = linux
            continue
        if PYTHON_PROGRAM_RE.match(prog):
            _toks, script_at, _ = _executed_script_at(tokens)
            options = tokens[idx + 1:script_at] if script_at > idx else tokens[idx + 1:]
            i = 0
            while i < len(options):
                opt = options[i]
                if opt == "-X" and i + 1 < len(options) and options[i + 1] == "utf8":
                    i += 2
                    continue
                if opt not in RISK_PYTHON_OPTIONS and opt != "--":
                    return f"it passes the interpreter option {opt}"
                i += 1
        return None
    return "it nests wsl.exe calls too deeply"


def _strip_git_message_data(command_line: str) -> str:
    """Replaces literal git commit/tag message arguments (-m '...', -m "...", --message='...', --message="...")
    with empty strings when they do not contain shell substitutions ($ or `), treating them as inert data."""
    def _repl(m: re.Match) -> str:
        content = m.group(1) if m.group(1) is not None else m.group(2)
        if "$" in content or "`" in content:
            return m.group(0)
        return ""

    pattern = re.compile(r"""(?<![\w-])(?:-m|--message)(?:\s+|=)?(?:'([^']*)'|"([^"]*)")""")
    return pattern.sub(_repl, command_line)


def _is_trade_engine_invocation(tokens: List[str], text: str) -> bool:
    """True when tokens/text invoke the trade execution engine as executable code, rather than referencing it as data
    in an issue reporter or inspection command."""
    if not TRADE_ENGINE_RE.search(text):
        return False
    for tok in tokens:
        for m in COMMAND_SUBSTITUTION_RE.finditer(tok):
            inner = m.group(1) or m.group(2) or ""
            if TRADE_ENGINE_RE.search(inner):
                return True
    prog = _program(tokens)
    toks, i, _ = _executed_script_at(tokens)
    script_name = os.path.basename(toks[i]).lower() if (toks and 0 <= i < len(toks)) else ""
    if (prog in ("report_issue.sh", "report_agent_issue.py")
            or script_name in ("report_issue.sh", "report_agent_issue.py")
            or prog in INSPECTION_PROGRAMS):
        return False
    return True


def _analyze_run_command(command_line: str, cwd: str, base_dir: str, shell: str = "bash") -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "deny": None, "force_ask": None, "trading": [], "trading_tokens": [], "batch": [], "risk_reducing": False,
        "risk_blocker": None, "all_safe": True, "record_eval": None, "ask_reason": None,
        "read_only_script": None, "read_only_blocker": None, "subcommands": 0,
    }
    if not command_line.strip():
        return result

    inline = _is_inline_code(command_line) or (shell == "powershell" and bool(PS_INLINE_CODE_RE.search(command_line)))
    subcommands = split_subcommands(command_line)
    result["subcommands"] = sum(1 for s in subcommands if s)

    # 1. Evaluation trail is immutable for the agent (record_evaluation.py --from-subagent writes it itself)
    eval_check_cmd = _strip_git_message_data(command_line)
    if EVALUATION_TRAIL_CMD_RE.search(eval_check_cmd):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Commands must not read or write "
            "logs/evaluations/, latest_dossier.json, Antigravity brain transcripts or Claude Code subagent "
            "transcripts. Use view_file (Claude Code: Read) to inspect the dossier. " + EVALUATOR_HINT
        )
        return result
    if TRANSCRIPT_ROOT_OVERRIDE_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): AGY_BRAIN_DIRS / CLAUDE_PROJECTS_DIRS "
            "are test-only overrides of the subagent transcript roots and cannot be set by the agent in a "
            "command. " + EVALUATOR_HINT
        )
        return result

    # 2. Inline code / piped interpreters using trading primitives
    if BASE64_EXEC_RE.search(command_line):
        result["deny"] = "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Obfuscated Execution): Decoded payloads piped into an interpreter are forbidden."
        return result
    if inline and INLINE_TRADING_PRIMITIVES_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Inline code (python -c, heredoc, piped interpreter) "
            "using trading primitives (execute_futures_trade, send_signed_request, /fapi/v1 write endpoints, MCP gateway) "
            f"is strictly forbidden. Orders must be routed exclusively through {CHOKE_POINT}; "
            "risk reduction must use the sanctioned CLI flags (--close-position, --move-breakeven, --auto-heal, "
            "--audit-orphans, --protect-pending) or scripts/loops/position_guardian_loop.py."
        )
        return result

    # 3. Raw HTTP writes to Binance
    if HTTP_CLIENT_RE.search(command_line) and BINANCE_HOST_RE.search(command_line) and HTTP_WRITE_RE.search(command_line):
        result["deny"] = (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Raw HTTP write requests to Binance "
            f"(fapi / MCP gateway) are strictly forbidden. Route orders through {CHOKE_POINT}."
        )
        return result

    # 4a. Ground-truth state written from inline code. Checked on the whole command line because heredoc bodies
    #     are split into many sub-commands (newlines, parentheses), separating the path from the write call.
    if inline and INLINE_WRITE_MARKERS_RE.search(command_line):
        named = _ground_truth_named(command_line)
        #     ... and git config / hook files named by a string literal (open('.git/hooks/pre-commit', 'w'))
        if any(_git_exec_config_path(m.group(2)) for m in INLINE_STRING_LITERAL_RE.finditer(command_line)):
            named.append(GIT_EXEC_CONFIG_KEY)
        if named:
            result["deny"] = ground_truth_denial(named)
            return result
    #     ... and inline code destroying / moving / aliasing the logs/ directory itself (shutil.rmtree('logs'))
    if inline:
        protected = _inline_logs_dir_writes(command_line, cwd, base_dir)
        #     ... and shell commands inline code runs (os.system('rm -rf logs'), subprocess.run(['rm', ...]))
        for nested in _inline_shell_strings(command_line):
            protected = protected or _ground_truth_line_hits(nested, cwd, base_dir, depth=1)
        if protected:
            result["deny"] = ground_truth_denial(protected)
            return result

    # 4b. Ground-truth state (GROUND_TRUTH_FILES) may only be written by its sanctioned desk script. A Bash line is
    #     judged whole, with `cd` / literal-variable tracking and xargs-pipeline analysis; a PowerShell statement
    #     (already split by the PowerShell scanner) is judged per sub-command.
    protected = _ground_truth_line_hits(command_line, cwd, base_dir, shell=shell)
    if protected:
        result["deny"] = ground_truth_denial(protected)
        return result

    for tokens in subcommands:
        text = " ".join(tokens)
        prog = _program(tokens)
        if prog in CHDIR_PROGRAMS:
            # The sanctioned scripts are matched against the hook's cwd: a line that changes directory never
            # auto-allows a risk-reducing call (one flat command per call)
            result["risk_blocker"] = result["risk_blocker"] or "it changes the working directory"

        # 5. Harness files require explicit confirmation
        if any(rx.search(text) and _subcommand_writes_path(tokens, text, rx, inline)
               for rx in (HARNESS_PATH_CMD_RE, GATE_PROGRAM_PATH_RE)):
            result["force_ask"] = HARNESS_WRITE_REASON
        if USER_PROFILE_SET_RE.search(text):
            result["force_ask"] = "Command changes the trading user profile (risk/autonomy/YOLO settings). Explicit confirmation required."

        # 6. Executing scripts outside scripts/ that embed trading primitives
        script = _unsanctioned_trading_script(tokens, cwd, base_dir)
        if script:
            result["deny"] = (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Script "
                f"'{os.path.basename(script)}' outside scripts/ uses trading primitives. "
                f"Orders must be routed exclusively through {CHOKE_POINT}."
            )
            return result

        # 7. Evaluation recorder
        if RECORD_EVALUATION_RE.search(text) and prog not in INSPECTION_PROGRAMS:
            from_subagent = any(t in ("--from-subagent", "--from-claude-subagent")
                                or t.startswith(("--from-subagent=", "--from-claude-subagent=")) for t in tokens)
            result["record_eval"] = {"from_subagent": from_subagent, "text": text}
            result["all_safe"] = False
            continue

        if prog in INSPECTION_PROGRAMS and not inline:
            result["all_safe"] = False if prog not in BENIGN_PROGRAMS else result["all_safe"]
            continue

        # 8. Trade openings (engine, batch deploy scripts, auto-deploy loops)
        is_batch = bool(DEPLOY_BATCH_RE.search(text)) or (
            bool(AUTO_DEPLOY_LOOP_RE.search(text)) and any(t.split("=", 1)[0] == "--auto-deploy" for t in tokens)
        )
        if is_batch:
            result["batch"].append(text)
            continue
        if _subcommand_is_risk_reducing(tokens, text, cwd, base_dir):
            # A sanctioned script with only its allowlisted flags is safe on its own (judged here, not skipped:
            # metacharacters, directory changes and redirects keep it from an auto-allow); every other sub-command
            # of the line still has to be safe below
            result["risk_reducing"] = True
            result["risk_blocker"] = result["risk_blocker"] or _risk_auto_allow_blocker(tokens)
            if _redirect_targets(tokens):
                result["all_safe"] = False
            continue
        read_only_key = _read_only_script_key(tokens, cwd, base_dir)
        if read_only_key:
            # Auto-allowed only as the single sub-command of the line (_evaluate_shell_command); next to anything
            # else it is not "safe" (normal permission policy, as before issue #191)
            result["read_only_script"] = result["read_only_script"] or read_only_key
            result["read_only_blocker"] = (result["read_only_blocker"]
                                           or _read_only_blocker(read_only_key, tokens, cwd, base_dir))
            result["all_safe"] = False
            continue
        if _is_trade_engine_invocation(tokens, text):
            unwrapped_list = _unwrap_subcommand(tokens)
            engine_subs = [s for s in unwrapped_list if _is_trade_engine_invocation(s, " ".join(s))]
            if not engine_subs:
                engine_subs = [tokens]
            _, _, outer_in_wsl = _executed_script_at(tokens)

            for eng_tokens in engine_subs:
                eng_text = " ".join(eng_tokens)
                toks, i, in_wsl = _executed_script_at(eng_tokens)
                in_wsl = in_wsl or outer_in_wsl
                script_path = toks[i] if (toks and 0 <= i < len(toks)) else ""
                sanctioned = _sanctioned_script(script_path, cwd, base_dir or find_workspace_root(), in_wsl, _windows_side_cwd(cwd))
                if not sanctioned and script_path:
                    result["ask_reason"] = f"Script path '{script_path}' is not the sanctioned repository script; user confirmation required."
                flags = _flags(eng_tokens)
                if flags & EXECUTOR_MOVE_BREAKEVEN_FLAGS and _symbol_count(eng_text) != 1:
                    result["deny"] = (
                        "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Structured Risk Parsing): `execute_futures_trade.py "
                        "--move-breakeven` requires exactly one --symbol (e.g. --move-breakeven --symbol BTCUSDT)."
                    )
                    return result
                non_opening = EXECUTOR_READ_ONLY_FLAGS | EXECUTOR_RISK_FLAGS | set(HELP_FLAGS)
                opening_text = eng_text
                if flags & EXECUTOR_MOVE_BREAKEVEN_FLAGS:
                    # --is-yolo (also abbreviated) next to --move-breakeven never opens: a break-even call that is not
                    # the exact sanctioned one-liner (another path, an unknown flag) asks, it is never sent to the gates
                    opening_text = " ".join(t for t in eng_tokens if not _is_breakeven_yolo_spelling(t))
                if not _executor_opening_named(opening_text) and any(
                        f in non_opening or (len(f) > 3 and any(o.startswith(f) for o in non_opening)) for f in flags):
                    # Read-only listing, or an exit / help that is not the exact sanctioned one-liner (another path, an
                    # unknown flag, a nested shell): normal permission policy (ask), never auto-allowed
                    result["all_safe"] = False
                else:
                    result["trading"].append(eng_text)
                    result["trading_tokens"].append(eng_tokens)
            continue

        if prog not in BENIGN_PROGRAMS or _redirect_targets(tokens):
            result["all_safe"] = False

    return result


# =============================================================================
# Binance MCP evaluation (read-only allowlist)
# =============================================================================
def split_binance_tool(name: str) -> Tuple[str, str, str]:
    """Returns (namespace, operation, verb) for dotted (futures_usds.newOrder) or verb-style names."""
    name = (name or "").strip()
    m = BINANCE_VERB_RE.match(name)
    if m and "." not in name:
        return m.group(2), m.group(3), m.group(1).lower()
    if "." in name:
        ns, op = name.rsplit(".", 1)
        return ns, op, ""
    return "", name, ""


def is_binance_call(server: str, tool: str) -> bool:
    server_l = (server or "").lower()
    if "binance" in server_l:
        return True
    return bool(BINANCE_NAMESPACE_RE.match(tool or "") or BINANCE_VERB_RE.match(tool or ""))


def _order_is_reduce_only(order_args: dict) -> bool:
    return _is_true(_first(order_args, "reduceOnly", "reduce_only", "reduce-only")) or \
        _is_true(_first(order_args, "closePosition", "close_position", "close-position"))


def evaluate_binance_tool(tool: str, mcp_args: dict, depth: int = 0) -> Tuple[str, str, Dict[str, Any]]:
    """
    Returns (verdict, reason, info) where verdict is one of:
      read_only | risk_reducing | leverage | deny
    """
    name = (tool or "").strip()
    info: Dict[str, Any] = {"tool": name, "args": mcp_args}
    deny_reason = (
        f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Direct call to Binance MCP write tool '{name}' "
        f"is strictly forbidden. Opening trades must be routed exclusively through the approved choke point: {CHOKE_POINT} "
        "to enforce the evaluator dossier, mechanical gates and atomic Stop Loss placement."
    )

    base = name.rsplit(".", 1)[-1] if name.startswith("binance.") else name
    if base in BINANCE_META_EXECUTE_TOOLS:
        if depth >= 3:
            return "deny", "🚨 FAIL-CLOSED: Nested tool_execute wrappers are not allowed.", info
        inner = _decode_str(_first(mcp_args, "toolName", "tool_name", "ToolName", "name"))
        inner_args = _decode_dict(_first(mcp_args, "arguments", "Arguments", "args") or {})
        if not inner:
            return "deny", "🚨 FAIL-CLOSED: Binance gateway tool_execute called without a target toolName.", info
        return evaluate_binance_tool(inner, inner_args, depth + 1)
    if base in BINANCE_META_READ_TOOLS:
        return "read_only", f"Binance gateway discovery tool '{name}' (read-only).", info

    ns, op, verb = split_binance_tool(name)
    op_l = op.lower()
    info.update({"namespace": ns, "operation": op})

    if verb == "get" or op_l in BINANCE_READ_ONLY_OPS:
        return "read_only", f"Binance read-only tool '{name}'.", info
    if "cancel" in op_l or op_l.startswith("delete"):
        return "risk_reducing", f"Risk-reducing action / exit authorized (Binance '{name}').", info
    if op_l == "changeinitialleverage":
        return "leverage", "", info

    is_futures = ns.startswith("futures")
    if is_futures and op_l in BINANCE_REDUCE_ONLY_ORDER_OPS and _order_is_reduce_only(mcp_args):
        return "risk_reducing", f"Risk-reducing action / exit authorized (reduce-only Binance '{name}').", info
    if is_futures and op_l in BINANCE_BATCH_ORDER_OPS:
        batch = _decode_value(_first(mcp_args, "batchOrders", "batch_orders", "batch-orders"))
        if isinstance(batch, list) and batch and all(isinstance(o, dict) and _order_is_reduce_only(o) for o in batch):
            return "risk_reducing", f"Risk-reducing action / exit authorized (reduce-only batch '{name}').", info
    return "deny", deny_reason, info


# =============================================================================
# Retired crypto_radar MCP server
# =============================================================================
def is_retired_mcp_server(server: str) -> bool:
    """True for the retired crypto_radar server, including plugin/alias prefixes (e.g. plugin_x_crypto_radar)."""
    norm = (server or "").strip().lower().replace("-", "_")
    return any(norm == s or norm.endswith("_" + s) for s in RETIRED_MCP_SERVERS)


def retired_radar_reason(tool: str) -> str:
    replacement = LEGACY_RADAR_TOOL_REPLACEMENTS.get(tool or "")
    hint = f"Use `{replacement}` instead. " if replacement else ""
    return (
        f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Retired MCP Server): The 'crypto_radar' MCP server has been retired "
        f"and tool '{tool or '?'}' is no longer available. {hint}"
        "Read-only analytics are CLI scripts with --json output (see .agents/skills/market-radar/SKILL.md); "
        f"orders and position management go exclusively through {CHOKE_POINT} "
        "(--positions, --move-breakeven, --close-position, --audit-orphans, --auto-heal, --protect-pending); trailing stops, dead-alpha "
        "and orphan audits run in scripts/loops/position_guardian_loop.py. Remove the stale 'crypto_radar' entry "
        "from your MCP client configuration."
    )


# =============================================================================
# File writes (evaluation trail, ground-truth state & harness protection)
# Ground-truth files are matched by suffix (logs/<name>) on the workspace-relative path, the normalised absolute
# path and the raw target (backslashes converted, Windows drive stripped), so `logs/x`, `./logs/x`, POSIX
# absolute paths and `C:\...\logs\x` (Claude Code on Windows feeding the WSL hook) are all denied. NTFS aliases
# (trailing dots/spaces, `::$DATA` streams) are stripped first, and the target is also resolved with
# os.path.realpath / os.path.samefile so symlinked directories and hard links to the files are denied too.
# =============================================================================
def _normalize_target(path: str, base_dir: str) -> Tuple[str, str]:
    """Returns (absolute normalized path with forward slashes, workspace-relative path or '')."""
    p = (path or "").strip()
    if p.lower().startswith("file://"):
        p = re.sub(r"^file:/*", "/", p, flags=re.IGNORECASE)
        if re.match(r"^/[A-Za-z]:", p):
            p = p[1:]
    p = os.path.expanduser(p)
    if not os.path.isabs(p):
        p = os.path.join(base_dir, p)
    abs_norm = os.path.normpath(p).replace("\\", "/")
    base_norm = os.path.normpath(base_dir).replace("\\", "/").rstrip("/")
    rel = ""
    if abs_norm.lower() == base_norm.lower():
        rel = ""
    elif abs_norm.lower().startswith(base_norm.lower() + "/"):
        rel = abs_norm[len(base_norm) + 1:]
    return abs_norm, rel


def _linked_worktree_rel(abs_path: str, base_dir: str) -> str:
    """Path of abs_path relative to the root of a linked git worktree of the same repository as base_dir, or ''.
    Pure filesystem reads (no git): the nearest ancestor with a .git entry must hold a .git file whose gitdir is
    <common git dir of base_dir>/worktrees/<name> and whose <gitdir>/gitdir points back to that .git file."""
    def _read(path: str) -> str:
        with open(path, "rb") as f:
            data = f.read(4097)
        if len(data) > 4096:
            raise ValueError("git pointer file too large")
        return data.decode("utf-8")

    def _resolve(value: str, rel_to: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("empty git pointer")
        return os.path.realpath(value if os.path.isabs(value) else os.path.join(rel_to, value))

    def _gitdir_of(dot_git_file: str) -> str:
        m = re.match(r"^gitdir:\s*(.+)$", (_read(dot_git_file).splitlines() or [""])[0])
        if not m:
            raise ValueError("not a gitdir pointer")
        return _resolve(m.group(1), os.path.dirname(dot_git_file))

    try:
        base_git = os.path.join(base_dir, ".git")
        if os.path.isdir(base_git):
            common = os.path.realpath(base_git)
        elif os.path.isfile(base_git):
            base_gitdir = _gitdir_of(base_git)
            commondir = os.path.join(base_gitdir, "commondir")
            if not os.path.isfile(commondir):
                return ""
            common = _resolve(_read(commondir), base_gitdir)
        else:
            return ""
        host = _host_path(abs_path, base_dir)
        cur = os.path.dirname(host)
        for _ in range(64):
            dot_git = os.path.join(cur, ".git")
            if os.path.lexists(dot_git):
                break
            parent = os.path.dirname(cur)
            if parent == cur:
                return ""
            cur = parent
        else:
            return ""
        # A symlinked .git (F/.git -> W/.git) is not a worktree: never follow the .git entry itself
        if os.path.islink(dot_git) or not os.path.isfile(dot_git):
            return ""
        gitdir = _gitdir_of(dot_git)
        same = lambda a, b: os.path.normcase(a) == os.path.normcase(b)  # noqa: E731
        if not same(os.path.dirname(gitdir), os.path.realpath(os.path.join(common, "worktrees"))):
            return ""
        back = os.path.join(gitdir, "gitdir")
        root = os.path.realpath(cur)
        if not os.path.isfile(back) or not same(_resolve(_read(back), gitdir), os.path.join(root, ".git")):
            return ""
        if same(root, os.path.realpath(base_dir)):
            return ""
        rel = os.path.relpath(os.path.realpath(host), root).replace("\\", "/")
    except (OSError, ValueError, UnicodeError):
        return ""
    return "" if rel == "." or rel == ".." or rel.startswith("../") else rel


def _strip_windows_aliases(path: str) -> str:
    """NTFS aliases of the same file: trailing dots/spaces of each component and alternate data streams
    (guardian_state.json. / guardian_state.json::$DATA / name:stream). The drive letter is kept."""
    p = re.sub(r"^/(?=[A-Za-z]:)", "", (path or "").replace("\\", "/"))
    m = re.match(r"^[A-Za-z]:", p)
    drive, rest = (p[:2], p[2:]) if m else ("", p)
    parts = []
    for comp in rest.split("/"):
        comp = comp.split(":", 1)[0]
        parts.append(comp if comp in (".", "..") else comp.rstrip(". "))
    return drive + "/".join(parts)


def _ground_truth_file_target(target: str, abs_norm: str, rel: str) -> Optional[str]:
    """GROUND_TRUTH_FILES key when a file-tool target ends with logs/<protected name>, else None."""
    raw = re.sub(r"^file:/*", "/", (target or "").strip(), flags=re.IGNORECASE)
    for candidate in (rel, abs_norm, raw):
        path = _shell_path(_strip_windows_aliases(candidate or ""))
        m = GROUND_TRUTH_TARGET_RE.search(path) if path else None
        if m:
            return GROUND_TRUTH_BASENAMES[m.group(1).lower()]
    return None


def _host_path(target: str, base_dir: str) -> str:
    """File-tool target as a path on the hook's own filesystem (C:\\x -> /mnt/c/x and /c/x -> /mnt/c/x under WSL)."""
    p = re.sub(r"^file:/*", "/", (target or "").strip(), flags=re.IGNORECASE)
    p = re.sub(r"^/(?=[A-Za-z]:)", "", p)
    if os.name != "nt":
        m = re.match(r"^([A-Za-z]):[\\/]", p)
        if m:
            p = f"/mnt/{m.group(1).lower()}/" + p[3:].replace("\\", "/")
        m = re.match(r"^/([A-Za-z])(?=/)", p)
        if m and not os.path.isdir(p[:2]) and os.path.isdir(f"/mnt/{m.group(1).lower()}"):
            p = f"/mnt/{m.group(1).lower()}" + p[2:]
    p = os.path.expanduser(p)
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def _ground_truth_alias_target(target: str, base_dir: str) -> Optional[str]:
    """GROUND_TRUTH_FILES key when a target reaches a protected file through a symlinked directory or file
    (ln -s logs st; Write st/guardian_state.json) or a hard link (os.path.realpath / os.path.samefile)."""
    try:
        host = _host_path(target, base_dir)
        real = os.path.realpath(host)
    except (OSError, ValueError):
        return None
    for key in GROUND_TRUTH_FILES:
        protected = os.path.join(base_dir, *key.split("/"))
        try:
            if os.path.normcase(real) == os.path.normcase(os.path.realpath(protected)):
                return key
            if os.path.exists(host) and os.path.exists(protected) and os.path.samefile(host, protected):
                return key
        except (OSError, ValueError):
            continue
    m = GROUND_TRUTH_TARGET_RE.search(_shell_path(_strip_windows_aliases(real)))
    return GROUND_TRUTH_BASENAMES[m.group(1).lower()] if m else None


def _git_exec_config_file_target(target: str, abs_norm: str, rel: str, base_dir: str) -> bool:
    """A file-tool target that is a git config / hook file (GIT_EXEC_CONFIG_PATH_RE) by its workspace-relative,
    normalised absolute (~ expanded) or raw path, or once symlinks are resolved."""
    raw = re.sub(r"^file:/*", "/", (target or "").strip(), flags=re.IGNORECASE)
    candidates = [rel, abs_norm, raw]
    try:
        candidates.append(os.path.realpath(_host_path(target, base_dir)))
    except (OSError, ValueError):
        pass
    for candidate in candidates:
        path = _shell_path(_strip_windows_aliases(candidate or ""))
        if path and GIT_EXEC_CONFIG_PATH_RE.search(path.lower()):
            return True
    return False


def evaluate_file_write(target: str, content: str, base_dir: str) -> Tuple[str, str]:
    if not target:
        return "force_ask", "File write without a resolvable target path."
    abs_norm, rel = _normalize_target(target, base_dir)
    rel_l = rel.lower()
    # Raw target too: a Windows path (C:\repo\logs\evaluations\x) is not absolute for a hook running under WSL
    raw = _shell_path(_strip_windows_aliases(re.sub(r"^file:/*", "/", target.strip(), flags=re.IGNORECASE)))
    if (rel_l == "logs/evaluations" or rel_l.startswith("logs/evaluations/") or EVALUATION_TRAIL_TARGET_RE.search(raw)
            or BRAIN_PATH_RE.search(abs_norm) or CLAUDE_SUBAGENT_PATH_RE.search(abs_norm)):
        return "deny", (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Writing to logs/evaluations/, "
            "Antigravity brain transcripts or Claude Code subagent transcripts is forbidden. " + EVALUATOR_HINT
        )
    protected = _ground_truth_file_target(target, abs_norm, rel) or _ground_truth_alias_target(target, base_dir)
    if protected:
        return "deny", ground_truth_denial([protected])
    if rel_l in HARNESS_FILES or any(rel_l.startswith(d) for d in HARNESS_DIRS):
        return "force_ask", (f"'{rel}' is a trading harness file / gate module (hooks / dossier provenance / "
                             "evaluator / executor gates). Explicit confirmation required.")
    if _git_exec_config_file_target(target, abs_norm, rel, base_dir):
        return "force_ask", (f"'{rel or abs_norm}' is a git config / hook file: a later git command runs what it "
                             "configures (core.fsmonitor, hooks, aliases). Explicit confirmation required.")
    if content and WRITE_ENDPOINT_PRIMITIVES_RE.search(content):
        return "force_ask", f"'{rel or abs_norm}' contains order-placing primitives. Explicit confirmation required."
    if content and not (rel_l.startswith("scripts/") or rel_l.startswith("tests/")) and SCRIPT_TRADING_PRIMITIVES_RE.search(content):
        # scripts/ and tests/ of a linked worktree of this repository are in scope too (issue #148)
        wt_rel = "" if rel else _linked_worktree_rel(target, base_dir).lower()
        if not (wt_rel.startswith("scripts/") or wt_rel.startswith("tests/")):
            return "force_ask", f"'{rel or abs_norm}' (outside scripts/) contains trading primitives. Explicit confirmation required."
    return "ask", ""


# =============================================================================
# Trade gates (opening orders through the sanctioned choke point)
# =============================================================================
def _tier_s_calibration_message(cand: dict, env: str, user_prof: dict, base_dir: str,
                                now_ts: Optional[int] = None) -> Optional[str]:
    """utils.score_calibration.tier_s_confirmation_required (issue #202); fails closed (asks) when unavailable."""
    if scal is None:
        tokens = str(cand.get("tier") or "").upper().replace("TIER", " ").split()
        if tokens and tokens[0] == "S":
            return ("Tier S score bucket not calibrated (calibration module unavailable): ask the user and rerun "
                    "with --confirmed.")
        return None
    try:
        return scal.tier_s_confirmation_required(cand, env, user_prof, base_dir, now=now_ts)
    except Exception as e:  # fail closed: ask the user (same text as the executor)
        return scal.confirmation_reason(cand.get("score"), f"calibration check failed ({type(e).__name__})")


def _pending_entry_symbols(base_dir: str, env: str) -> Tuple[set, Optional[str]]:
    """(symbols, error) of the logs/pending_entries.json records whose target_env is env (issue #48; stdlib json,
    no network, no executor import). A missing file is no symbols; an unreadable or malformed file (root or
    "entries" not a JSON object) is an error (PROD denies, TESTNET ignores it)."""
    path = os.path.join(base_dir, "logs", "pending_entries.json")
    if not os.path.exists(path):
        return set(), None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return set(), f"pending entries registry unreadable ({e})"
    if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
        return set(), "pending entries registry malformed"
    return {str(r.get("symbol")).upper() for r in data["entries"].values()
            if isinstance(r, dict) and r.get("target_env") == env and r.get("symbol")}, None


def evaluate_trade_opening(cmd: str, args: dict, mcp_args: dict, base_dir: str,
                           conversation_id: Optional[str], env_hint_cmd: str = "",
                           tokens: Optional[List[str]] = None) -> Tuple[str, str]:
    now_ts = int(time.time())
    target_sym = extract_target_symbol(cmd, args)

    explicit_env = extract_env_argument((env_hint_cmd or cmd), args)
    try:
        env = resolve_env(explicit_env, base_dir=base_dir)
    except ValueError as ve:
        return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}"
    is_prod = env == "prod"

    try:
        import user_profile as up
        user_prof = up.load_user_profile(base_dir=base_dir)
    except Exception:
        user_prof = {}

    # PRODUCTION INVARIANT: NO GATE BYPASSES IN PROD
    has_bypass_eval = ("--bypass-eval-gate" in cmd or "--bypass_eval_gate" in cmd
                       or _is_true(args.get("bypass_eval_gate")) or _is_true(mcp_args.get("bypass_eval_gate")))
    has_bypass_delta = ("--bypass-delta-gate" in cmd or "--bypass_delta_gate" in cmd
                        or _is_true(args.get("bypass_delta_gate")) or _is_true(mcp_args.get("bypass_delta_gate")))
    if is_prod and (has_bypass_eval or has_bypass_delta):
        return "deny", (
            "🚨 PROD INVARIANT VIOLATION: Gate bypasses (--bypass-eval-gate, --bypass-delta-gate) "
            "are strictly FORBIDDEN in PROD environment."
        )

    # Only what the executor would parse as --confirmed / --user-confirmed (agy/MCP args: executor truthiness)
    is_confirmed = executor_confirmed(cmd, tokens)
    for key in ("confirmed", "user_confirmed"):
        if _arg_truthy(args.get(key)) or _arg_truthy(mcp_args.get(key)):
            is_confirmed = True

    # USER PROFILE GATES: AUTONOMOUS TIER S & YOLO SLOT
    if is_prod and not user_prof.get("autonomous_execution_tier_s", False):
        if not is_confirmed:
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Autonomous Execution Disabled):\n"
                "Autonomous Tier S execution is disabled in user profile (autonomous_execution_tier_s=False).\n"
                "Human confirmation is required in PROD before opening new positions.\n"
                "👉 Ask the user to confirm and add the '--confirmed' flag; only the user may change "
                "autonomous execution in config/user_profile.json."
            )

    std_lev, _ceiling, _yolo_cap = leverage_limits(user_prof)

    if tokens is not None:
        unwrapped = [s for s in _unwrap_subcommand(tokens) if TRADE_ENGINE_RE.search(" ".join(s))]
        exec_tokens = unwrapped[0] if unwrapped else list(tokens)
    else:
        subs = [s for seg in split_subcommands(cmd) for s in _unwrap_subcommand(seg)]
        matching_subs = [s for s in subs if TRADE_ENGINE_RE.search(" ".join(s))]
        exec_tokens = matching_subs[0] if matching_subs else _tokenize(cmd)

    idx = _program_index(exec_tokens)
    while idx < len(exec_tokens) and not TRADE_ENGINE_RE.search(exec_tokens[idx]):
        idx += 1
    scoped_tokens: List[str] = []
    start_i = (idx + 1) if idx < len(exec_tokens) else 0
    for tok in exec_tokens[start_i:]:
        if tok.startswith("#") or _is_redirect(tok):
            break
        scoped_tokens.append(tok)

    is_yolo_cli = any(bool(IS_YOLO_FLAG_RE.search(tok)) for tok in scoped_tokens)
    is_yolo_trade = (is_yolo_cli
                     or _arg_truthy(args.get("is_yolo")) or _arg_truthy(mcp_args.get("is_yolo")))

    lev_flag_present = False
    lev_val_str: Optional[str] = None
    i = 0
    while i < len(scoped_tokens):
        tok = scoped_tokens[i]
        if tok == "--leverage":
            lev_flag_present = True
            if i + 1 < len(scoped_tokens):
                if (scoped_tokens[i + 1] == "("
                        and i + 3 < len(scoped_tokens)
                        and scoped_tokens[i + 3] == ")"
                        and re.match(r"^\d+$", scoped_tokens[i + 2])):
                    lev_val_str = scoped_tokens[i + 2]
                    i += 3
                else:
                    lev_val_str = scoped_tokens[i + 1]
                    i += 1
            else:
                m_paren = re.search(r"--leverage(?:\s+|=)\(\s*(\d+)\s*\)", env_hint_cmd or cmd)
                lev_val_str = m_paren.group(1) if m_paren else ""
        elif tok.startswith("--leverage="):
            lev_flag_present = True
            lev_val_str = tok.split("=", 1)[1]
        i += 1

    trade_lev = 3
    if lev_flag_present:
        if lev_val_str == PS_NESTED_PLACEHOLDER:
            trade_lev = std_lev
        elif not re.match(r"^\d+$", lev_val_str or ""):
            return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): leverage must be a literal number."
        else:
            trade_lev = int(lev_val_str)
    elif "leverage" in mcp_args or "leverage" in args:
        mcp_lev = mcp_args.get("leverage") if "leverage" in mcp_args else args.get("leverage")
        mcp_lev_str = str(_decode_value(mcp_lev)).strip()
        if not re.match(r"^\d+$", mcp_lev_str):
            return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): leverage must be a literal number."
        trade_lev = int(mcp_lev_str)

    if trade_lev > std_lev:
        is_yolo_trade = True
    if is_yolo_trade and not user_prof.get("yolo_slot_enabled", False):
        return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (YOLO Slot Disabled): YOLO moonshot slot is disabled in user profile."
    bounds_err = check_leverage_bounds(trade_lev, user_prof, is_yolo=is_yolo_trade)
    if bounds_err:
        return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): " + bounds_err

    if not target_sym:
        return "deny", "🚨 FAIL-CLOSED: Unable to determine the order symbol deterministically. Exactly one --symbol is required."

    trade_dir, dir_err = parse_trade_direction(cmd, mcp_args or args)
    if not trade_dir and (is_prod or not has_bypass_delta):
        return "deny", f"🚨 FAIL-CLOSED (Direction Gate): {dir_err} Order blocked."

    # GATE 1: MANDATORY CLEAN-ROOM EVALUATOR (provenance-verified dossier)
    if not has_bypass_eval:
        ok, dossier_reason, cand = check_dossier(target_sym, trade_dir, env, base_dir, conversation_id, now_ts=now_ts)
        if not ok:
            return "deny", (
                "🚨 ACTION BLOCKED BY PRE-TOOL-USE HOOK (Clean-Room Evaluator Required):\n"
                f"{dossier_reason}\n"
                "Executing orders directly in primary chat without a fresh, provenance-verified 'APPROVED' "
                "dossier (< 20 min) is STRICTLY PROHIBITED.\n👉 " + EVALUATOR_HINT
            )
        if _candidate_is_yolo(cand, truthy=_arg_truthy) and not user_prof.get("yolo_slot_enabled", False):
            return "deny", "🚨 BLOCKED BY PRE-TOOL-USE HOOK (YOLO Slot Disabled): Candidate requires YOLO moonshot slot, which is disabled in user profile."
        # Mirror of execute_futures_trade.enforce_evaluation_dossier (PROD), same order (issue #63)
        if is_prod and isinstance(cand, dict) and _arg_truthy(cand.get("requires_user_confirmation")) and not is_confirmed:
            return "deny", (
                f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (User Confirmation Required): The evaluator approved {target_sym} "
                "pending explicit user confirmation (requires_user_confirmation=true).\n"
                + CONFIRM_RE_RUN_HINT
            )
        if is_prod and (is_yolo_trade or _candidate_is_yolo(cand, truthy=_arg_truthy)) and not is_confirmed:
            return "deny", (
                f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (YOLO Confirmation): {target_sym} is a YOLO entry. YOLO entries "
                "are never fast-tracked (not even with autonomous_execution_tier_s) and always require explicit "
                "user confirmation in PROD.\n"
                + CONFIRM_RE_RUN_HINT
            )
        # Issue #202: an uncalibrated Tier S score bucket asks the user like Tier A+/A (mirror of the executor;
        # same helper and message; local store only, no network). Never a rejection once --confirmed.
        if is_prod and isinstance(cand, dict) and not is_confirmed:
            calib_msg = _tier_s_calibration_message(cand, env, user_prof, base_dir, now_ts)
            if calib_msg:
                return "deny", (
                    f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Uncalibrated Tier S Score): {target_sym}: {calib_msg}\n"
                    + CONFIRM_RE_RUN_HINT
                )

    # GATE 2: DELTA-NEUTRAL & SESSION STATE AUDIT (cache-based pre-check, docstring 5: no network in the hook; the
    # executor re-reads the exchange and its live-anchored gates are authoritative)
    if not has_bypass_delta:
        state_file = os.path.join(base_dir, "logs", "session_state.json")
        if not os.path.exists(state_file):
            return "deny", "🚨 FAIL-CLOSED: session_state.json does not exist. Portfolio delta cannot be audited before executing the order."
        try:
            with open(state_file, "r", encoding="utf-8") as f:
                state = json.load(f)
        except Exception as e:
            return "deny", f"🚨 FAIL-CLOSED: Critical error reading session_state.json ({str(e)}). Order blocked."
        if not isinstance(state, dict):
            return "deny", "🚨 FAIL-CLOSED: session_state.json is not a valid JSON object. Order blocked."

        if state.get("is_valid") is False or "error" in state:
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is flagged INVALID ({state.get('error', 'Binance synchronization error')}). Order blocked."
        if is_prod and state.get("is_valid") is not True:
            return "deny", "🚨 FAIL-CLOSED: session_state.json lacks the 'is_valid': true required to trade in PROD. Order blocked."

        try:
            last_updated_ts = int(state.get("last_updated_ts", 0))
        except (TypeError, ValueError):
            last_updated_ts = 0
        age_seconds = now_ts - last_updated_ts if last_updated_ts > 0 else (now_ts - int(os.path.getmtime(state_file)))
        if is_prod and (last_updated_ts <= 0 or age_seconds > 300):
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is STALE ({age_seconds}s > 300s limit in PROD). Run 'python3 scripts/sync_session_state.py' before trading."
        if age_seconds > 300:
            return "deny", f"🚨 FAIL-CLOSED: session_state.json is STALE ({age_seconds}s > 300s). Re-synchronize the session state."

        max_open_positions = int(user_prof.get("max_open_positions", 3))
        portfolio = state.get("portfolio_exposure", {}) or {}
        total_active = portfolio.get("total_active_positions")
        if total_active is None:
            total_active = len(state.get("active_positions", []))
        try:
            total_active = int(total_active)
        except (TypeError, ValueError):
            total_active = 0
        # Issue #48: pending resting entries hold a slot too (same-env registry symbols without an open position).
        pending_syms, reg_err = _pending_entry_symbols(base_dir, env)
        if reg_err and is_prod:
            return "deny", (f"🚨 FAIL-CLOSED: {reg_err}; cannot count pending resting entries "
                            "(logs/pending_entries.json) for the Max Open Positions Gate. Order blocked. "
                            "Repair or restore logs/pending_entries.json ('python3 scripts/execute_futures_trade.py "
                            "--protect-pending' reports the registry error).")
        active_syms = {str(p.get("symbol")).upper() for p in state.get("active_positions") or []
                       if isinstance(p, dict) and p.get("symbol")}
        pending_count = len(pending_syms - active_syms)
        if total_active + pending_count >= max_open_positions:
            return "deny", (
                f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Max Open Positions Gate): "
                f"Active positions ({total_active}) + pending resting entries ({pending_count}) reached or exceeded "
                f"maximum limit ({max_open_positions}) configured in user profile."
                + (" Pending entries are counted from logs/pending_entries.json without an exchange read: a stale "
                   "record is cleared by 'python3 scripts/execute_futures_trade.py --protect-pending' or the "
                   "position guardian once its entry order has been gone for 60 s." if pending_count else "")
            )

        # Issue #48: the sync's delta incl. resting entries when known, else the filled-only delta_bias.
        delta_bias = portfolio.get("delta_bias_incl_resting")
        if is_prod and delta_bias in (None, "UNKNOWN") and pending_syms - active_syms:
            # PROD: the sync could not measure (or, issue #160, did not report: key missing) the resting entries the
            # registry says exist (fail closed).
            return "deny", (
                "🚨 FAIL-CLOSED (Delta-Neutral Hard Gate): the resting-entry exposure is UNKNOWN in "
                f"session_state.json (delta_bias_incl_resting {'missing' if delta_bias is None else 'UNKNOWN'}) "
                "while logs/pending_entries.json has "
                f"{len(pending_syms - active_syms)} pending entr(y/ies) without an open position "
                f"({', '.join(sorted(pending_syms - active_syms))}). Run 'python3 scripts/sync_session_state.py' "
                "(and 'python3 scripts/execute_futures_trade.py --protect-pending') before opening orders."
            )
        if not delta_bias or delta_bias == "UNKNOWN":
            delta_bias = portfolio.get("delta_bias", "NEUTRAL")
        if delta_bias == "LONG_HEAVY" and trade_dir == "LONG":
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                f"Portfolio is bullishly unbalanced (Delta: +${portfolio.get('net_notional_delta_usdt', 0):.2f} USDT / LONG_HEAVY). "
                "Opening additional Longs without Short hedging is strictly prohibited."
            )
        if delta_bias == "SHORT_HEAVY" and trade_dir == "SHORT":
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Delta-Neutral Hard Gate): "
                f"Portfolio is bearishly unbalanced (Delta: -${abs(portfolio.get('net_notional_delta_usdt', 0)):.2f} USDT / SHORT_HEAVY). "
                "Opening additional Shorts without Long hedging is strictly prohibited."
            )

    return "allow", "Mechanical hard gates and subagent validation PASSED successfully."


# =============================================================================
# Decision engine
# =============================================================================
def evaluate_shell_command(command_line: str, cwd: str, base_dir: str, conversation_id: Optional[str],
                           shell: str = "bash", wsl_depth: int = 0) -> Tuple[str, str]:
    """(decision, reason) for a shell command line (Bash, or PowerShell text normalised by scan_powershell). An
    "allow" (risk-reducing exit or gated trade opening) needs a flat single-line command (_auto_allow_blocker);
    otherwise it is downgraded to "ask". Bash: the joined arguments of each wsl.exe call without -e/--exec are
    judged once more as the Linux default shell re-parses them (_bash_wsl_shell_commands, nested wsl calls up to
    NESTED_DEPTH_LIMIT levels); the most restrictive result wins."""
    if wsl_depth > NESTED_DEPTH_LIMIT:
        return "deny", (f"🚨 FAIL-CLOSED: wsl.exe calls nested deeper than {NESTED_DEPTH_LIMIT} levels cannot be "
                        "audited.")
    decision, reason = _evaluate_shell_command(command_line, cwd, base_dir, conversation_id, shell)
    if decision == "allow":
        blocker = _auto_allow_blocker(command_line)
        if blocker:
            decision, reason = "ask", (reason + f" Not auto-allowed because {blocker}; user confirmation "
                                                "required.").strip()
    if shell != "bash" or decision == "deny":
        return decision, reason
    results = [(decision, reason)]
    for line in _bash_wsl_shell_commands(command_line):
        results.append(evaluate_shell_command(line, cwd, base_dir, conversation_id, "bash", wsl_depth + 1))
        if results[-1][0] == "deny":
            return results[-1]
    return max(results, key=lambda r: PS_DECISION_RANK.get(r[0], PS_DECISION_RANK["deny"]))


def _evaluate_shell_command(command_line: str, cwd: str, base_dir: str, conversation_id: Optional[str],
                            shell: str = "bash") -> Tuple[str, str]:
    analysis = analyze_run_command(command_line, cwd, base_dir, shell=shell)
    if analysis["deny"]:
        return "deny", analysis["deny"]

    record_eval = analysis["record_eval"]
    if record_eval and not record_eval["from_subagent"]:
        try:
            env = resolve_env(extract_env_argument(record_eval["text"]) or extract_env_argument(command_line), base_dir=base_dir)
        except ValueError as ve:
            return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}"
        if env == "prod":
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Evaluation Trail Protection): Manual dossier recording is disabled "
                "in PROD. " + EVALUATOR_HINT
            )

    if analysis["batch"]:
        try:
            env = resolve_env(extract_env_argument(command_line), base_dir=base_dir)
        except ValueError as ve:
            return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}"
        if env == "prod" or analysis["trading"] or len(analysis["batch"]) > 1:
            return "deny", (
                "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Batch deploy scripts and auto-deploy loops "
                "open positions that the per-trade evaluator dossier cannot verify. Route each order through "
                "'scripts/execute_futures_trade.py' after a clean-room evaluation."
            )
        analysis["trading"] = analysis["batch"]
        analysis["trading_tokens"] = []

    if len(analysis["trading"]) > 1:
        return "deny", (
            "🚨 BLOCKED BY PRE-TOOL-USE HOOK (Choke Point Enforcement): Only one trade opening per command is allowed "
            "so that each order is gated individually."
        )

    if analysis["trading"]:
        sub = analysis["trading"][0]
        sub_tokens = analysis["trading_tokens"][0] if analysis["trading_tokens"] else None
        decision, reason = evaluate_trade_opening(sub, {"CommandLine": sub}, {}, base_dir, conversation_id,
                                                  env_hint_cmd=command_line, tokens=sub_tokens)
        # A risk-reducing sub-command that may not be auto-allowed on its own never rides on a gated opening (a
        # line without one keeps the trade gates' decision: `cd repo && <opening>` is judged as before)
        if decision == "allow" and (not analysis["all_safe"]
                                    or analysis["risk_reducing"] and analysis["risk_blocker"]):
            decision = "force_ask" if analysis["force_ask"] else "ask"
            reason = reason + " Compound command contains other sub-commands; user confirmation required."
        return decision, reason

    if analysis["force_ask"]:
        return "force_ask", analysis["force_ask"]
    read_only_key = analysis.get("read_only_script")
    if read_only_key and analysis.get("subcommands") == 1 and not analysis["risk_reducing"] \
            and not analysis["record_eval"]:
        if analysis.get("read_only_blocker"):
            return "ask", (f"Read-only analysis script ({read_only_key}), not auto-allowed because "
                           f"{analysis['read_only_blocker']}: run it as one plain call with its own flags and paths "
                           "inside logs/; user confirmation required.")
        return "allow", READ_ONLY_ALLOW_REASON.format(read_only_key)
    if analysis["risk_reducing"] and analysis["all_safe"]:
        if analysis["risk_blocker"] == FORCED_BREAKEVEN_BLOCKER:
            return "ask", f"Forced break-even, not auto-allowed: {FORCED_BREAKEVEN_BLOCKER}; user confirmation required."
        if analysis["risk_blocker"]:
            return "ask", (f"Risk-reducing action / exit, not auto-allowed because {analysis['risk_blocker']}: run "
                           "the sanctioned command as one flat call; user confirmation required.")
        return "allow", "Risk-reducing action / exit authorized."
    return "ask", analysis.get("ask_reason") or ""


def evaluate_powershell_command(command: str, cwd: str, base_dir: str, conversation_id: Optional[str],
                                depth: int = 0) -> Tuple[str, str]:
    """(decision, reason) for a Claude Code PowerShell command, fail closed:
    1. scan_powershell (quotes, escapes, comments, nested bodies); unparseable text is denied;
    2. the whole command (bodies inlined): payload denials (encoded payloads, raw HTTP to Binance) and the backstop;
    3. the statement with its bodies replaced by a placeholder, judged exactly like Bash, plus the joined arguments
       of each wsl.exe call without -e/--exec judged as a Bash command line (its default shell re-parses them);
    4. when there are bodies: the whole command through the Bash analysis (deny only) and every body
       ({...}, $(...), @(...), (...)) recursively, up to NESTED_DEPTH_LIMIT levels (deeper is denied).
    Results combine most-restrictive-wins (deny > force_ask > ask > allow); a command with nested bodies is never
    auto-allowed (risk-reducing auto-allow needs a flat command)."""
    def fail(why: str) -> Tuple[str, str]:
        return "deny", f"🚨 FAIL-CLOSED: PowerShell command could not be parsed for the pre-trade checks ({why})."

    if depth > NESTED_DEPTH_LIMIT:
        return fail(f"blocks nested deeper than {NESTED_DEPTH_LIMIT} levels")
    try:
        outer, full, bodies = scan_powershell(command)
        payload_deny = powershell_payload_denial(full)
        if payload_deny:
            return "deny", payload_deny
        ps_deny, ps_force_ask = powershell_backstop(full, cwd, base_dir)
        wsl_lines = _powershell_wsl_shell_commands(outer)
    except Exception as e:
        return fail(str(e))
    results = [evaluate_shell_command(outer, cwd, base_dir, conversation_id, shell="powershell")]
    if results[0][0] == "deny":
        return results[0]
    if ps_deny:
        return "deny", ps_deny
    # wsl.exe without -e/--exec: the Linux default shell re-parses the joined arguments, judged as Bash
    for line in wsl_lines:
        results.append(evaluate_shell_command(line, cwd, base_dir, conversation_id))
        if results[-1][0] == "deny":
            return results[-1]
    if ps_force_ask:
        results.append(("force_ask", ps_force_ask))
    if bodies:
        whole = evaluate_shell_command(full, cwd, base_dir, conversation_id, shell="powershell")
        if whole[0] == "deny":
            return whole
        for body in bodies:
            results.append(evaluate_powershell_command(body, cwd, base_dir, conversation_id, depth + 1))
            if results[-1][0] == "deny":
                return results[-1]
    decision, reason = max(results, key=lambda r: PS_DECISION_RANK.get(r[0], PS_DECISION_RANK["deny"]))
    if bodies and decision == "allow":
        return "ask", (reason + " The command contains nested PowerShell blocks; user confirmation required.").strip()
    if decision == "allow" and ("\n" in command.rstrip() or "\r" in command.rstrip()):
        return "ask", (reason + " The command spans several lines; user confirmation required.").strip()
    if decision == "allow" and "`" in command:
        return "ask", (reason + " The command contains a PowerShell backtick escape; user confirmation "
                                "required.").strip()
    return decision, reason


def evaluate_payload(payload: dict) -> Tuple[str, str, str]:
    """Returns (decision, reason, tool_label). decision in allow|deny|ask|force_ask."""
    call = normalize_tool_call(payload)
    tool_label = call["tool"] or "?"
    base_dir = find_workspace_root()
    conversation_id = payload.get("conversationId") if isinstance(payload.get("conversationId"), str) else None
    if conversation_id is None and "toolCall" not in payload and isinstance(payload.get("session_id"), str):
        # Claude Code: the evaluator subagent transcript records the parent session as its sessionId
        conversation_id = payload["session_id"] or None

    # ---------------------------------------------------------------- file writes
    if call["kind"] == "file_write":
        decision, reason = evaluate_file_write(call["target_file"], call["content"], base_dir)
        return decision, reason, tool_label

    # ---------------------------------------------------------------- MCP calls
    if call["kind"] == "mcp":
        server, mcp_tool, mcp_args = call["server"], call["mcp_tool"], call["mcp_args"]
        tool_label = f"{server}:{mcp_tool}" if server else mcp_tool
        server_norm = (server or "").lower().replace("-", "_")
        wrapped_binance = mcp_tool in BINANCE_META_EXECUTE_TOOLS and is_binance_call(
            "", _decode_str(_first(mcp_args, "toolName", "tool_name", "ToolName", "name"))
        )

        # Retired crypto_radar MCP server (any tool) and its legacy tool names on any server alias.
        if is_retired_mcp_server(server_norm) or mcp_tool in LEGACY_RADAR_TOOL_REPLACEMENTS:
            return "deny", retired_radar_reason(mcp_tool), tool_label

        if is_binance_call(server, mcp_tool) or wrapped_binance:
            verdict, reason, info = evaluate_binance_tool(mcp_tool, mcp_args)
            if verdict == "read_only":
                return "ask", reason, tool_label
            if verdict == "risk_reducing":
                return "allow", reason, tool_label
            if verdict == "deny":
                return "deny", reason, tool_label
            # leverage gate
            lev_args = info.get("args") or {}
            raw_lev = _decode_value(lev_args.get("leverage"))
            try:
                requested_lev = int(float(raw_lev))
            except (TypeError, ValueError):
                return "deny", f"🚨 BLOCKED BY PRE-TOOL-USE HOOK (Leverage Gate): Invalid leverage value ({raw_lev}).", tool_label
            symbol = extract_target_symbol("", lev_args)
            explicit_env = extract_env_argument("", lev_args)
            try:
                resolve_env(explicit_env, base_dir=base_dir)
            except ValueError as ve:
                return "deny", f"🚨 FAIL-CLOSED (Environment Resolution): {str(ve)}", tool_label
            allowed, reason = check_leverage_gate(symbol, requested_lev, base_dir, target_env=explicit_env,
                                                  conversation_id=conversation_id)
            return ("allow" if allowed else "deny"), reason, tool_label

        return "ask", "", tool_label

    # ---------------------------------------------------------------- shell commands
    if call["kind"] == "run_command":
        with _audit_scope():  # one work budget for every statement / body / wsl line of this command
            if call["shell"] == "powershell":
                decision, reason = evaluate_powershell_command(call["command"], call["cwd"], base_dir,
                                                               conversation_id)
            else:
                decision, reason = evaluate_shell_command(call["command"], call["cwd"], base_dir, conversation_id)
        return decision, reason, tool_label

    # ---------------------------------------------------------------- anything else
    return "ask", "", tool_label


# =============================================================================
# Output contracts
# =============================================================================
def _write_heartbeat(mode: str, tool: str, decision: str) -> None:
    """Best-effort liveness signal for trading_doctor; never changes the decision."""
    try:
        path = os.environ.get(HEARTBEAT_ENV_OVERRIDE) or os.path.join(find_workspace_root(), "logs", "hook_heartbeat.json")
        now = time.time()
        record = {
            "hook": HOOK_NAME,
            "mode": mode,
            "last_seen_ts": int(now),
            "last_seen_utc": datetime.datetime.fromtimestamp(now, datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            "tool": tool,
            "decision": decision,
        }
        if atomic_write_json is not None:
            atomic_write_json(path, record)
        else:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(record, f)
            os.replace(tmp, path)
    except Exception:
        pass


def emit_decision(decision: str, reason: str = "", code: int = None, mode: str = "legacy") -> int:
    """Emits the decision in the runtime's contract and returns the process exit code."""
    if mode == "agy":
        res = {"decision": decision}
        if reason:
            res["reason"] = reason
        print(json.dumps(res))
        return 0

    if mode == "claude":
        if decision == "deny":
            sys.stderr.write((reason or "Blocked by pre_trade_guard.") + "\n")
            return 2
        if decision in ("allow", "force_ask"):
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "allow" if decision == "allow" else "ask",
                    "permissionDecisionReason": reason or "",
                }
            }))
        return 0

    res = {"decision": decision}
    if code is not None:
        res["code"] = code
    elif decision == "deny":
        res["code"] = 2
    if reason:
        res["reason"] = reason
    print(json.dumps(res))
    return res.get("code", 0) if decision == "deny" else 0


def _detect_mode(argv: List[str], payload: Any) -> str:
    if "--agy" in argv:
        return "agy"
    if isinstance(payload, dict) and "toolCall" not in payload and ("tool_name" in payload or "tool_input" in payload):
        return "claude"
    return "legacy"


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = "agy" if "--agy" in argv else "legacy"
    tool_label = "?"
    try:
        raw_input = sys.stdin.read()
        if not raw_input.strip():
            decision, reason = "deny", "🚨 FAIL-CLOSED: Empty payload received by pre-trade guard."
        else:
            try:
                payload = json.loads(raw_input)
            except Exception as e:
                payload = None
                decision, reason = "deny", f"🚨 FAIL-CLOSED: Invalid JSON payload ({str(e)})."
            if payload is not None:
                mode = _detect_mode(argv, payload)
                if mode == "claude":
                    tool_label = str(payload.get("tool_name") or "?")
                    if not isinstance(payload.get("tool_name"), str) or not payload.get("tool_name"):
                        decision, reason = "deny", "🚨 FAIL-CLOSED: Missing or invalid tool_name in payload."
                    else:
                        decision, reason, tool_label = evaluate_payload(payload)
                elif not isinstance(payload, dict) or not isinstance(payload.get("toolCall"), dict):
                    decision, reason = "deny", "🚨 FAIL-CLOSED: Unknown or malformed payload shape (missing toolCall object)."
                elif not payload["toolCall"].get("name") or not isinstance(payload["toolCall"].get("name"), str):
                    decision, reason = "deny", "🚨 FAIL-CLOSED: Missing or invalid tool name in toolCall."
                else:
                    decision, reason, tool_label = evaluate_payload(payload)
    except Exception as e:
        sys.stderr.write(f"[PRE-TRADE-GUARD INTERNAL ERROR] {str(e)}\n")
        decision, reason = "deny", f"🚨 FAIL-CLOSED: Pre-trade guard internal error ({str(e)}). Cannot verify safety — order blocked."

    _write_heartbeat(mode, tool_label, decision)
    try:
        return emit_decision(decision, reason, mode=mode)
    except Exception:
        return 0 if mode == "agy" else 2


if __name__ == "__main__":
    exit_code = main()
    if "--agy" in sys.argv[1:]:
        # Antigravity contract: decision is conveyed via JSON stdout; always exit 0.
        sys.exit(0)
    sys.exit(exit_code if exit_code is not None else 0)
