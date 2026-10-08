# Full control check before the pilot

- Linear: [ENG-163](https://linear.app/rolando-projects/issue/ENG-163/qualify-the-full-control-path-before-the-pilot), which blocks ENG-161 (the pilot)
- Checks every row of the [governance map](governance-map.md) against evidence: `python3 -m controller.audit`
- The full table, one row per control with its evidence: [control-audit-table.md](control-audit-table.md) (regenerate with `python3 -m controller.audit --out docs/control-audit-table.md`)
- Status on 2026-10-08: **the pilot stays blocked.** 39 of the 59 controls are observed, and the 3 advisory-only ones are labeled honestly. The other 17 are held, by 20 open items in total, which fall into the three groups below.

## What this is

Every control the pilot relies on is listed in the governance map, with the test that shows it refusing the unsafe case. `controller/audit/controls.py` names the evidence for each one. That evidence is the offline tests and red-team cases, which the audit runs on the spot, plus records of what was seen live, with the date and the exact configuration. The audit stops the pilot unless:

- every map row appears exactly once, with the same class the map gives it (so "advisory" can't quietly become "enforced", or the other way round);
- Code and Detective rows have a passing test or red-team case;
- Platform and Boundary rows have a dated live record naming the configuration;
- Human-gate rows have either a test or a live record;
- nothing is pending.

Advisory rows need only a note on what is recorded about them. Nothing else relies on them.

## What still holds the pilot

### 1. The bypass-test live session (owned by the "Tests that try to sneak past checks" thread)

This is one worker fire on a new ordinary sample task, run by Rolando after PR #21 (the loop fixes) merges. It covers:

- G-A8, G-F5, G-F6: the planted-text and release cases. A PR comment is treated as data. A release run for anything but main is refused, and publishing waits for Rolando.
- G-B2, G-B4: a second check also named `verified`, on a probe PR that edits a workflow. It must be flagged as a control change, and the release must refuse it. Also: the collector reads each run's source from GitHub.
- G-E2: a push after Rolando approves dismisses the approval, and the loop's checks become stale.
- G-B3, G-D9, G-E1, G-E3, G-E4: re-observing the fixed loop on a real PR. A clearing note now has to name what it clears, and an observation has to be about the check.
- G-G4: the factory account's session list shows only the session that fire started, and no session from merges.

### 2. Decisions on the map wording (Rolando)

Each of these rows describes something that isn't true of the live setup. Each needs the map to say what is true, or a change to the setup:

- **G-E5:** the verifier reads GitHub with Rolando's own login and posts its review as him. "Read-only" comes from the code (GET only) and from procedure, not from a separate read-only account.
- **G-F1:** the main ruleset keeps a repo-admin bypass for pull requests, which is how Rolando merges his own PRs. The map asks for an empty bypass list.
- **G-F5:** the bot can start a Release run. The run then waits for Rolando's approval before publishing anything, so "trigger: denied" should read "publish: denied".
- **G-B5:** a worker's own check run never counts as evidence; only CI on the exact commit does. So comparing a cloud run with CI on a failing example is proposed as not needed for the pilot.
- **G-D8:** a merge before the loop's "ready" is now detected and recorded (PR #21) but not prevented. Merging stays Rolando's call on GitHub.

### 3. One screenshot (Rolando)

- **G-C11:** the factory account's usage-credit (overage) setting, showing it off.

## Smaller drifts noted, not holding

- The map says usage over 80% blocks dispatch. Code and `docs/limits.md` stop at 75%, which is stricter (G-C14).
- The release path moved to the separate site repo and its `release` environment. The map and the pilot's CLAUDE.md still name `github-pages` (G-F4).
- The Claude app installation shows "All repositories". The bot is still limited to the pilot repo by its collaborator rights (G-G1).
- Run-time thresholds only alert. New dispatch is still blocked while a run is overdue, because that run holds the single lane (G-C8).
- The map's G-F6 row has its owner and mechanism in one cell. The audit reads its class from the 4th column.
- `planning/eng-158/offline-results.md` is older than the current red-team run, which blocks all 124 offline cases.

## Blocked modes (governance map section 5), all confirmed off

| Mode | Still off because | Evidence |
|---|---|---|
| Automated repair / auto-fix | A repair needs Rolando's signed go-ahead; no code turns on auto-fix or subscribes to PRs | G-C2 tests; G-G5 record: each factory worker PR has exactly one commit, pushed before it opened; `python3 -m controller.audit --autofix` re-checks every worker PR |
| Automatic retry of an unknown launch | Recovery never re-fires; a re-fire needs a definite not-launched and his record | G-C12 and G-D5 tests |
| Rollout under Rolando's own identity | Worker PRs are authored by the bot | G-F2 and G-G1 records |
| Release on merge | Release is started by hand, for main only, and waits for his approval | G-F4 records; release live case pending |
| Local check fallback | The loop reads only the trusted CI run for the exact commit | G-B2 tests |
| Parallel lanes, intake, inbox, DAG | One lane (G-D4). The controller has no intake, inbox or Grok code. A contract's `depends_on` is checked as data and nothing schedules by it | G-D4 tests; code search on 2026-10-08 |

## Configuration this ran against

- Factory controller: rNavarrete/software-factory branch `claude/eng-163-control-check-jyyyqw` (on main `8bd6ebb` plus PR #21). The table records the exact commit.
- Pilot repo: rNavarrete/factory-pilot-demo, main `6704028`, main ruleset 24692198, `release` environment (Rolando as the only reviewer).
- Worker: routine `trig_01CHWbQ267i1CMLGUym1kGd9` on the factory account, environment `factory`.
- Fires this week: about 6 of 12. The audit itself fires nothing and reads no ledger.

## Operator recovery instructions

- **A launch whose outcome is unknown, or an attempt that has to be closed:** [controller/recovery/reconcile-procedure.md](../controller/recovery/reconcile-procedure.md). Nothing re-fires by itself. The lane is freed only by a signed clearing that meets ADR 0002 section 6.1.
- **Stopping a running worker:** [controller/attempts/stop-procedure.md](../controller/attempts/stop-procedure.md). This is advisory: there is no API to stop a cloud session, so it's done by hand.
- **A PR merged before the loop said ready:** the close-out run says so and records it as `merged-before-ready` in the ledger. Nothing more is needed. The pilot's numbers count it.
- **Main moved after a PR was checked:** the loop says which review no longer counts. The review is posted again for the new main, and CI runs again on the new main.
- **Re-running this check:** `python3 -m controller.audit` (exit 0 only when nothing holds); `python3 -m controller.audit --autofix` for the auto-fix detector.

## Rolando's active time on ENG-163

| Date | Minutes | What |
|---|---|---|
| 2026-10-08 | 0 | Nothing asked of him yet |
