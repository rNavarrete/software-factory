# Attempt limits, budgets and honest usage metrics

- Linear: [ENG-138](https://linear.app/rolando-projects/issue/ENG-138/define-enforceable-attempt-limits-and-honest-usage-metrics)
- Status: **Accepted** (Rolando approved as written 2026-10-08, see sign-off record)
- Bounded by: [ADR 0001](adr/0001-operating-model.md) (accepted), [ADR 0003](adr/0003-pilot-and-value-thresholds.md) (accepted), [governance map](governance-map.md) (accepted). Uses the runtime facts in [ADR 0002](adr/0002-runtime-controller-identity.md) (still Proposed; section 10 says what to re-check if it changes).
- Moves to `docs/limits.md` in the factory repo once ENG-185 creates it (implementation plan section 4).
- Built by: ENG-146 (caps, threshold blocks, hold), ENG-147 (ledger), ENG-176 (dispatch path), ENG-182 (fire adapter). ENG-183 buys the factory plan and supplies the evidence that the overage setting is applied; **this document records the overage decision** (governance map G-C11). Used by ENG-162.
- Review: drafted, then checked by a separate critic agent against ADR 0001-0003 and the governance map; its 20 findings are fixed in this version.

## 1. What this sets, in plain terms

The factory gets a small, fixed allowance: a few tries per task, a weekly cap on how many worker runs it can start, a fixed monthly subscription with pay-as-you-go overage switched off, and the 2 hours a week of Rolando's upkeep time already set in ADR 0001. When an allowance runs out, the controller **refuses to start new work** and asks Rolando. It cannot stop a run that is already going, so anything about running sessions is an alert for Rolando, not a guarantee.

## 2. Enforced, advisory and unsupported

This uses a subset of the governance map's section 2 classes. Only rows marked as enforcement may be relied on.

| Class | Meaning here |
|---|---|
| **Code** | The controller checks the ledger in the dispatch path and refuses to fire. It only ever blocks **new** launches |
| **Platform** | An account setting the worker cannot change (usage credits off, G-C11) |
| **Boundary** | The worker has no path or credential to the thing (the ledger, G-D2) |
| **Human gate** | Nothing moves until Rolando records a decision |
| **Advisory** | An alert, a written procedure or a prompt instruction. May help, never relied on. Covers stopping an active cloud session (G-C9) and run wall-time alerts |
| **Unsupported** | No mechanism exists at all. Covers per-run turn limits, per-run token or dollar limits, and a maximum run time for cloud sessions |

No public API cancels a running cloud session. The UI offers archive and delete, and their effect on a running worker is still unknown (ADR 0002 §4 "Stop an active worker"; Appendix A step 7 will record it). The routines docs, checked 2026-10-07, document no turn limit and no maximum run time ([routines](https://code.claude.com/docs/en/routines), [cloud environments](https://code.claude.com/docs/en/cloud-environments)). So **no limit in this document claims to end a running session.** Any turn guidance written into the routine prompt is Advisory.

## 3. Limits per task, per attempt and per week

| Limit | Value | Class | How it works | Governance ID |
|---|---|---|---|---|
| **Attempts per task** | **3 total, including the first** | Code + Human gate | An attempt is reserved in the ledger, in the same transaction as the dispatch intent, **before** the fire call. Every reserved attempt counts, whether or not it reaches a branch or PR. Attempts 2 and 3 each need Rolando's repair authorization naming the failure and attempt number. A fourth attempt is an exception: a fresh contract approval with a new budget (ADR 0001 §4) | G-C1, G-C2, G-A3, G-C5 |
| **Fires per attempt** | **2** (the original fire plus one re-fire) | Code + Human gate | A re-fire is allowed only after a *definite* `not-launched` result (400, 401, 403, 404, 429; ADR 0002 §6), only with Rolando's re-fire decision record naming the prior fire, and keeps the same attempt number because no session was created. Such a re-fire does not consume the approval (G-A6) and is not a repeated trigger (G-D6). G-D6 still applies to `launched` and `launch-outcome-unknown`. A `launch-outcome-unknown` fire is **never** re-fired; it holds the lock until Rolando reconciles (G-D5). If the second fire is also `not-launched`, the attempt ends as `not-launched`, and going on needs a new attempt (repair authorization, G-C2) | Proposed G-C12 |
| **Fires per task** | **6** (3 attempts × 2 fires) | Code | A task cannot use more fires than its attempts allow. Reaching the cap escalates once (as G-C5) | Proposed G-C12 |
| **Fires per rolling 7 days** (whole factory) | **12** | Code | Counted from timestamped fire rows in the ledger. Every fire counts, including re-fires, `launch-outcome-unknown` fires, and qualification or test fires made through the factory routine (ADR 0002 Appendix A, ENG-145, ENG-158, ENG-163). 12 fits the 4 factory-arm pilot tasks at 3 attempts each **with no re-fires**; reaching it blocks dispatch until the window rolls or Rolando records a raised cap. The platform's own caps (30 fires/hour per routine, 100 API fires/hour per account) are far above this | Proposed G-C13 (a G-C8 threshold) |
| **Active attempts at once** | **1** | Code | Single-writer lock (ADR 0001 §7). No new attempt while a prior one is running, unresolved or `launch-outcome-unknown` | G-D4, G-D5 |
| **Hold** | On or off | Code + Human gate | `factory hold <reason>` writes a hold record and every dispatch refuses while it is set. Only `factory resume` with Rolando's note clears it. The controller sets a hold automatically only for subscription exhaustion (§5). Rolando sets it by hand for the upkeep cap (§4) or anything else | Proposed G-C14 (implements G-C7) |
| **Run wall time** | Alert at **45 min**; **90 min** marks the run overdue | Advisory | Compared with the fire time whenever `factory status` or `factory reconcile` runs. No daemon polls, and it cannot stop the run. An overdue run still holds the lock, so nothing new starts | G-C9 |

**Per task wall time.** ADR 0002 §4 says budgets are by "run count and wall time per task". That is met by the per-run wall-time alert above plus the attempt and fire caps: a task can have at most 3 launched runs, each alerted on at 45 minutes. The ledger also reports the total wall time across a task's runs (exact, from fire time to marker PR or reconcile).

CI re-runs, extra CI jobs and review comments are **not** attempts. Attempts are counted only at dispatch (G-C4).

### Weekly envelope

| Part | Allowance | Class |
|---|---|---|
| Worker runs | 12 fires per rolling 7 days (above) | Code |
| Plan usage | Dispatch refuses unless the latest usage snapshot is at most 7 days old and shows the weekly usage bar under 75% (§4) | Code on the recorded snapshot; the reading is human |
| Subscription money | One fixed plan price per month, usage credits **off**, so variable spend is $0 (§6) | Platform |
| Infrastructure money | $0 expected (public repo, so GitHub Actions and Pages are free; no separate cloud VM charge) | Advisory, checked monthly against invoices |
| Rolando's upkeep time | 2 active hours/week during the pilot (ADR 0001 §8; no second number set here) | Human gate |

### Requested governance-map amendment

The accepted governance map has no row for the fire caps or the hold. So that ENG-163's audit exercises them, this ticket asks the map's owner to add:

| ID | Control | Class | Ticket | Negative test |
|---|---|---|---|---|
| G-C12 | Fires per attempt (2) and per task (6) are capped; a re-fire needs a definite `not-launched` and Rolando's re-fire record | Code + Human gate | ENG-146, ENG-176 | Force `not-launched` on fires 1 and 2 of an attempt and request a third: **refused**. Re-fire after `launch-outcome-unknown`: **refused** |
| G-C13 | Rolling 7-day fire cap (12) | Code | ENG-146 | Seed 12 fire rows in the last 7 days, restart the controller, request a dispatch: **refused**, alert recorded |
| G-C14 | Hold blocks all dispatch until `factory resume`; stale or high usage snapshot blocks dispatch | Code | ENG-146 | Set a hold and request dispatch: **refused**. Snapshot 8 days old, or weekly 80%: **refused** |

## 4. Alert thresholds and what can actually be done at each

"Not yet tested" means ENG-146 or ENG-163 must exercise the action before the pilot. An alert is only recorded (and shown by `factory status`); it does not block. Blocking happens only at the "Hard stop" column.

| Signal | Alert at | Hard stop at | Action available | Class of the action | Status |
|---|---|---|---|---|---|
| Attempts on one task | 2 of 3 used | 3 of 3 | No new attempt; one escalation with prior attempts, state, last error and next decision (G-C5) | Code | Not yet tested (ENG-146) |
| Fires on one task | 5 of 6 | 6 of 6 | No new fire; one escalation | Code | Not yet tested (G-C12) |
| Fires in rolling 7 days | 9 of 12 (75%) | 12 of 12 | Next dispatch refused, alert recorded | Code | Not yet tested (G-C13) |
| Plan usage (weekly bar on claude.ai/settings/usage) | Snapshot shows 60% or more | Snapshot shows 75% or more, **or** latest snapshot older than 7 days | Dispatch refused until a fresh snapshot under 75% is recorded or the window resets | Code on the recorded snapshot; the reading is human | Not yet tested (G-C14) |
| Subscription exhausted | — | Exhaustion detected (§5) | Automatic hold and one escalation; no retry | Code | Not yet tested (G-C7). The real platform signal is unknown until the first exhaustion |
| Rate limited (429) | — | Every 429 | Factory-wide earliest-fire time from `Retry-After` (§5) | Code | Not yet tested (G-C6) |
| Usage credits spent | Any spend in a snapshot | — | Rolando turns credits off again and records why they were on | Setting is Platform; detection is manual (§7 snapshot) | Not yet verified: ENG-183's settings screenshot, plus G-C11's test that an exhausted plan **blocks** rather than bills, seen at the first exhaustion |
| Run wall time | 45 min | none (90 min = overdue, lock stays held) | Rolando archives (or deletes) the session in the claude.ai UI from the URL in the ledger, then runs `factory reconcile`. Best effort: whether archiving stops a running worker is unknown | **Advisory** | Not yet tested (ADR 0002 Appendix A step 7; ENG-146 writes the stop procedure that separates disabling future launches, stopping monitoring and confirming the writer stopped) |
| Upkeep time this week | 90 min (75%) | 120 min | ADR 0001 §8: flag, then pause. **Default here:** pilot dispatch pauses too; Rolando sets `factory hold` until he decides | Human gate (G-C8 does not cover human-time caps) | Process only |
| Setup time | 30 h (75%) | 40 h | ADR 0001 §8 | Human gate | Process only |

## 5. Rate limits and subscription exhaustion

**Rate limit (HTTP 429 from the fire API).** The fire API returns `429 rate_limit_error` with a `Retry-After` header when a routine passes 30 fires/hour or the account passes 100 API fires/hour ([fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire), checked 2026-10-07). The adapter's HTTP client has retries switched off. On a 429 the controller:

1. records the fire as `not-launched`, reason `rate-limited`;
2. stores a **factory-wide** `retry_not_before = now + Retry-After`, which blocks every fire, not only this attempt's;
3. if `Retry-After` is missing or unparseable, uses 15 minutes, doubling for each consecutive 429 across the factory, capped at 2 hours;
4. refuses any fire before `retry_not_before`, even if Rolando asks;
5. at the next fire, re-runs every dispatch check from scratch (approval valid and not revoked, attempt and fire budgets, snapshot, hold, lock). A re-fire also needs Rolando's re-fire record (§3, ADR 0002 §6).

The controller never loops on its own: one fire per command, and a 429 always ends the command.

**Subscription exhaustion.** The docs say only that, without usage credits, "additional runs are rejected until your usage window resets" ([routines](https://code.claude.com/docs/en/routines)). The fire API documents no specific error for it, and nothing documents what a session does if the limit hits mid-run. So the controller sets a `subscription-exhausted` hold if any of these happen:

- a non-200 fire response whose body mentions a usage, session, weekly or spend limit;
- Rolando records the reconcile outcome `stopped-usage-limit` because the session shows a limit message;
- Rolando sets the hold himself after reading the usage page.

Matching text in the response body only **adds** the hold. It never changes the launch state, which stays decided by HTTP status alone (ADR 0002 §6): a 5xx carrying usage text is still `launch-outcome-unknown` (lock held, no re-fire), and the fire still counts toward the per-task and weekly caps. On exhaustion the controller writes one hold and one escalation, with the reset time if known, and refuses all dispatch until `factory resume`. It never waits and retries by itself. The ENG-182 adapter stores the raw body of every non-200 fire, so the first real exhaustion shows the true signal; this section is then updated.

## 6. Money

Prices and plan facts checked 2026-10-07; ENG-183 confirms them at purchase.

| Item | Proposed | Notes and source |
|---|---|---|
| Factory subscription | **Claude Pro, $20/month** (billed monthly), on the dedicated factory account | Routines are "available on Pro, Max, Team, and Enterprise plans" ([routines](https://code.claude.com/docs/en/routines)); price from [claude.com/pricing](https://claude.com/pricing). The pilot is single-lane, at most 12 runs a week, on a tiny repo. Upgrade to **Max 5x at $100/month** ([Max plan](https://support.claude.com/en/articles/11014257-about-claude-s-max-plan-usage)) only by a recorded decision, and only if Pro's limits block the pilot more than once. A plan change is a requalification trigger (ADR 0002 §9 item 4) |
| Usage credits (metered overage) | **Off** | Set at claude.ai/settings/usage on the factory account ([usage credits](https://support.claude.com/en/articles/12429409)). Whether it starts off is not stated by the docs, so ENG-183 checks and screenshots it. With credits off an exhausted plan **rejects** runs instead of billing, so budgets fail closed (G-C11). Credits also cut the prompt cache from one hour to five minutes ([costs](https://code.claude.com/docs/en/costs)), making each run dearer. If credits are ever turned on, it is a recorded decision with a monthly spend cap, never "unlimited" |
| Cloud compute | $0 | "There is no separate compute charge for the cloud VM" ([Claude Code on the web](https://code.claude.com/docs/en/claude-code-on-the-web)) |
| GitHub (Actions, Pages, rulesets, environments) | $0 | Public repo (ADR 0003) |
| Linear | Existing workspace, $0 extra | |
| Bot GitHub account | $0 | Free account (ENG-183) |

**Expected extra spend: $20/month, all of it fixed.**

## 7. Honest usage metrics

Every number the factory reports carries one of four labels. ENG-147 stores the label with the value, and ENG-162 prints it.

| Label | What it covers | Examples |
|---|---|---|
| **Exact** | Counted by the controller or read from GitHub | Attempts, fires, launch outcomes, holds, escalations, CI runs per commit, time from fire to the marker PR |
| **Recorded by Rolando** | Rolando's own log | Active minutes per task and per upkeep session; reconcile outcomes |
| **Approximate, account-wide** | Snapshots of the claude.ai usage page | Session and weekly usage %, usage-credit spend |
| **Unavailable** | Data the platform does not give | Tokens or dollars per routine run, task or attempt; why a run stopped if nobody saw it |

Rules:

- **Snapshots** are entered by hand with `factory snapshot`: Rolando reads claude.ai/settings/usage on the factory account. Each is stored as `{taken_at, session_pct, weekly_pct, credits_spent, source: "manual claude.ai/settings/usage", scope: "account-wide", attribution: "approximate"}`. One snapshot at most 7 days old is **required** for dispatch (§4, Code). Taking one before and after each task is recommended but **Advisory**.
- A before/after difference is **never** called the task's usage. Even on a dedicated account, chat use, other sessions or a window reset in between get mixed in. Reports say "account usage moved from X% to Y% during this task (approximate, account-wide)".
- Per-task and per-run tokens and cost are stored as `unavailable`, not as zero and not as an estimate. Local `/usage` only sees sessions on that machine, so it cannot see cloud runs and is not used ([costs](https://code.claude.com/docs/en/costs)).
- A missing snapshot stays missing; nobody back-fills one from memory.

## 8. Persistence

- All counters, fire rows, holds, escalations, the factory-wide `retry_not_before` and snapshots live in the controller's SQLite ledger at `~/.software-factory` on Rolando's workstation, outside every checkout (ADR 0002). The worker has no path or credential to it (G-D2).
- Counts are derived from ledger rows every time, never held in memory, so a restart cannot reset them. G-C3 is the test: after 2 attempts, kill and restart the controller, ask for 2 more; only 1 is allowed and there is still one escalation. G-C13 repeats this for the weekly fire cap.
- An attempt is counted when it is reserved, before the fire call. Work that never reaches a branch or PR still uses its attempt and its fire (G-C1).

## 9. Choices for Rolando

1. **Plan for the factory account:** Pro at $20/month (recommended), or Max 5x at $100/month.
2. **Overage:** usage credits **off** on the factory account (recommended), or on with a monthly cap.
3. **Allowances:** 3 tries per task, 12 worker runs per week, a 75% plan-usage stop with a weekly usage reading, and pausing pilot runs when his upkeep hits 2 hours in a week (recommended as written).

## 10. Inputs to ENG-162 and re-check triggers

ENG-162's comparison must include:

- the factory subscription for each month of the pilot, pro-rated by pilot days for part-months;
- subscription months paid **before** the pilot (from ENG-183's purchase), reported as setup cost, like the sunk time in ADR 0001 §8;
- any usage-credit spend (expected $0) and infrastructure cost (expected $0, checked against invoices);
- Rolando's upkeep minutes;
- which numbers are approximate or unavailable (§7).

The baseline arm runs on Rolando's existing plan, which he pays anyway, so the factory subscription is **extra** cost on the factory side only. Cost is reported alongside the result; it does not change ADR 0003 §6's 30% and 10% thresholds, which are decided on Rolando's minutes.

Re-check this document if: ADR 0002 changes the runtime or adapter; the exit path (ADR 0002 §8, local `claude -p`, which has a real time kill and per-run cost) is used; either account's plan or usage-credit setting changes; or the platform documents a cancel API, a turn limit or an exhaustion error.

## 11. Acceptance criteria trace (ENG-138)

| Criterion | Where |
|---|---|
| Per-task authorized attempts (default max 3 total, including the first), run fire counts, weekly subscription and upkeep envelope | §3 (table and weekly envelope), §6 |
| Time and usage alert thresholds with the verified action at each; unsupported cancellation and turn limits explicitly advisory | §2, §4 |
| Limits persist across controller restarts and include work before a PR exists | §8, §3 attempts row |
| Rate-limit rejection uses Retry-After and backoff; subscription exhaustion blocks dispatch and escalates without tight retries | §5 |
| No account-wide usage delta labeled exact per-task usage; unavailable data explicit | §7 |
| Second subscription and infrastructure cost in ENG-162's comparison; overage decision recorded | §6, §10, sign-off record below |

## Sign-off record

Sign-off applies to this file as it stands at the version below; any later edit to sections 3 to 7 or 9 needs a fresh sign-off.

- Version signed: `fb4a0b619ccd5361bb551f2964e99a0fdbb4d4277ee8f64671c18842a31f07c4` (sha256 of this file above the sign-off record)
- Decision: **Approve as written** (decision card in the ENG-138 thread, 2026-10-08 00:22Z, Rolando Navarrete). This records:
  1. Factory account plan: **Claude Pro, $20/month**.
  2. Overage: **usage credits off** on the factory account (the overage decision G-C11 asks ENG-138 to record). ENG-183 supplies the settings screenshot as evidence.
  3. Allowances: 3 attempts per task, 2 fires per attempt, 6 per task, 12 fires per rolling 7 days, 75% plan-usage stop with a snapshot at most 7 days old, 45-minute advisory run-time alert, pilot dispatch pauses at the 2 h/week upkeep cap.
- Open follow-up: governance-map amendment G-C12..G-C14 (section 3) handed to the coordinator; the map is unchanged until its owner applies it.
- Rolando's active time on ENG-138: about 3 minutes (reading the summary and one decision).
