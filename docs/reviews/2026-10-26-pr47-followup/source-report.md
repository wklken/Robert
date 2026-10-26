The PR has already been merged into `main`, and it has three bugs worth fixing in a follow-up. The most serious is that a single slow `git` command can stop all work for a repository, every cycle. There were no earlier review comments to reconcile.

I reviewed the source and test changes in full. Those are 30 source files (+704/−234) out of 126 changed files. The remaining ~10k lines are review-evidence archives under `docs/reviews/`, which I only spot-checked. I did not rerun the full test suite locally; CI shows all 13 checks passing. All three bugs below were reproduced with small scripts against the PR's final commit (`e7da717`).

## High

<a id="finding-1"></a>

**1. One command timing out fails the whole repository pipeline.** This is new in this PR.

- **Where it happens:**
  - [`_run_inspection_command`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/run_once.py#L259) now re-raises its 5-second `git status`/`diff` timeout.
  - [`_action_scope_for_result`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/run_once.py#L5149) no longer catches its 5-second `git rev-parse` timeout.
  - [`run_command`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/common.py#L10) applies a 60-second default even when no control deadline is set. `git fetch` and `git worktree add` previously had no timeout at all.
- **Why the handlers miss it:** the routing and audit handlers (`run_once.py` around lines 3630, 6462, 6919, 7029 and 7099) only catch `CalledProcessError` and `FileNotFoundError`. The timeout therefore reaches the catch-all [at line 7187](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/run_once.py#L7187). That marks the whole repository run as `execution_deadline` even though the deadline never ran out.
- **Reproduction:** with no deadline set, `_run_inspection_command(['sleep','6'], '.')` raises `TimeoutExpired` after 5.0 seconds.
- **Impact:** supervision is the first step after the lease is acquired. A crashed worker with a large worktree, or a first `git fetch` that takes over 60 seconds, makes the repository skip audit, publish, discovery and routing. The same attempt or event is picked up again next cycle, so this repeats every cycle. The new tests only cover a deadline that has already expired, not a single command timing out.
- **Suggested fix:** use a separate exception type for "control deadline used up". Handle a single command's timeout per item: skip that attempt, or record a route failure for that event and move on. Don't quietly impose the 60-second cap on `fetch`/`worktree add`, or make it configurable.

## Medium

<a id="finding-2"></a>

**2. A failing `ps` call now stops the resident daemon.** This is new in this PR.

- **Where it happens:** every [`run_child_json`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/daemon.py#L141) call now runs `_live_process_group`, which shells out to `ps`. That includes the `gh api rate_limit` query. Nothing catches `OSError` there.
- **Impact:**
  - If `ps` fails (for example BusyBox, which doesn't support `-axo`, or a fork failure), the daemon exits with status failed.
  - Startup runs the same check [in `_load_context`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/daemon.py#L408). That call sits outside `run_daemon`'s `try`, so a failure at startup ends in a traceback.
  - If `ps` itself times out, a child that already succeeded is reported as `child_timeout` and its result is dropped.
- **Reproduction:** I faked `ps` failing in both ways. With an `OSError`, `run_child_json` raises. With a timeout, a child that printed a successful result comes back as `ok=False, status=child_timeout`.
- **Suggested fix:** if the process-group check fails, treat it as "not confirmed stopped": skip lease cleanup and keep the child's real result.

<a id="finding-3"></a>

**3. Ordinary bug reports are now routed as `analysis`.** This is new in this PR.

- **Where it happens:** in the keyword fallback of [`infer_intent`](https://github.com/wklken/Robert/blob/e7da71767212d277872ed011b3594b48dc5de2f1/src/robert_agent/discover.py#L209-L216), analysis keywords are now checked before bug keywords. The analysis keywords (`方案`, `explain`, `discuss`) match anywhere in the text.
- **Before and after:** each of these routed to `bug_fix` before the PR and routes to `analysis` now:
  - `登录失败` / `报错 500，期望方案：恢复正常登录`
  - `Crash on save` / `Can someone explain why? Please fix it.`
  - `Bug: login broken` / `let's discuss the fix`
- **Why this looks unintended:** the PR's goal (explicit analysis requests) is already handled by the new line-start match. Flipping the fallback order is an extra behaviour change, and no test covers text that mentions both a bug and an analysis word.
- **Suggested fix:** restore the original fallback order, where bug words win. Keep the new line-start match and the word-boundary fix, and add regression cases for mixed text.

## Checked and fine

- The core-only publication check reads `core.remaining` from the parsed rate-limit data, so the key exists and the logic is correct.
- The new `notifications` indexes only use columns that have existed since the table was created, so older databases are not affected.
- Attempts that were already `completed` with a recorded PID before upgrade will be checked by supervision. Once their process is confirmed gone, they get their stop timestamp, so they won't hold capacity forever.

**Recommendation: fix in follow-up PRs, starting with the timeout issue.**

The full report is saved at `<temporary-review-directory>/report.md`, with the reproduction scripts (`repro.py`, `intent.py`) next to it. I have not posted it to the PR; tell me if you want it posted as a comment.
