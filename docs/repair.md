# Automatic repairs (ENG-160)

When the independent review fails a worker's PR for an ordinary implementation defect, the factory can start the next attempt on its own to fix it. It only does this within the repair allowance that came with Rolando's Todo move, and it never goes past the task's attempt budget. Anything that isn't a routine fix goes to Rolando instead, with one report.

This is off until Rolando turns it on. The pilot onboarding keeps `repair_allowance` at 0, so for now every repair still needs his typed go-ahead.

## What a repair is

A repair is a new attempt, not a fix-up of the old one. The new attempt works on the same approved contract (same goal, acceptance criteria, checks and permitted paths, and the same digest). It runs on a new branch `claude/<task>-a<n>` and opens a new PR, and that PR gets a fresh independent review. The worker receives what failed (the PR, the exact commit and the review's findings) in the `repair` part of its task message. The finding text is encoded so it can't close or fake any part of that message. The worker is told to treat the findings as evidence, never as instructions, never to weaken a test, check, criterion or workflow, and to list every finding id in its PR body. A finding counts as fixed only when the review of the new PR says so. The worker saying "fixed" counts for nothing.

## When the factory repairs on its own

All of these must hold, and each is checked where it can't be faked:

| Check | Where |
|---|---|
| The review's latest recorded verdict for this attempt is **failed**, on a known PR and commit. Passed, needs Rolando, incomplete, unknown, blocked or unreadable never starts a repair. | Service, from the review's own ledger records (`controller/repair/review.py`) |
| Every open blocking finding was routed to repair, is in a category known to be a routine fix, and doesn't ask to delete, skip, disable or weaken a test, check, criterion or workflow, or to change what a test expects. Nothing the review routed to Rolando is open, blocking or not; only a finding marked advisory counts as non-blocking. There are at most 20 findings, with at most 24,000 characters of text between them, and the worker's whole task message must fit its limit. | Service (`controller/repair/policy.py`) |
| The failed commit is still the head of the attempt's open PR. A newer push waits for a review of the new head. | Service, and again in the approval check |
| The next attempt fits the contract's budget and the project's `max_attempts`, and the repairs used so far (every attempt after the first, typed ones included) are fewer than the allowance. | Service, signer and approval check |
| Rolando has recorded that the earlier worker finished (the clearing below). | Recovery, as for any repair |
| Linear still shows the Todo move as Rolando's, on the same ticket text. The move was authorized for exactly this contract, and the project's repair settings are unchanged since the move. | Signer, which reads Linear itself |
| No other repair go-ahead exists under this move for this attempt number on a different failed commit. | Signer's own state file, saved before it signs |
| The signed go-ahead (`source-repair-authorized`) is fresh (30 minutes, never more than an hour), names the dispatcher's routine and the move's own allowance, and its findings are intact. A rejection or revocation of the contract, or the ticket leaving Todo, voids it. | Approval check at dispatch (`controller/approval/approval.py`) |

If any check fails, the factory posts one note on the ticket saying why and what Rolando can do: repair it by hand, merge the PR as it is after checking it, or close the attempt. It posts that note once per failed commit, never in a loop. When the task has used all its attempts, the note says further work needs a new ticket.

These go to Rolando and are never repaired automatically: anything the review routed to him, any category not on the routine list, any finding asking to weaken verification, and anything involving changed requirements, ambiguous behavior, architecture, security, permissions or protected controls.

## The one step that still needs Rolando: confirming the earlier worker stopped

A repair is a second writer on the same task, so the factory first needs proof that the earlier worker has finished. A PR, green CI or a quiet session are not that proof. The factory can't read a session's state or stop one, so Rolando confirms it. The ticket note gives the session link and the exact `fly ssh console` command to run on his Mac once he has checked that the session has finished, with the real link already filled in. Once that is recorded, the factory goes on by itself.

## The repair allowance

`repair_allowance` in the project's onboarding entry is how many of the task's attempts may be automatic repairs. They count inside `max_attempts`, not on top of it, so three attempts allow at most two repairs. A contract with a smaller budget gets less. A positive allowance needs `repair_allowance_since`, the time repairs were switched on. A Todo move made before that time is refused for any task that would get repairs, and Rolando moves the ticket to Todo again to accept the new terms.

The signer writes the allowance into the move's signed approval and keeps it in its own state file. Renewing the approval while the ticket waits never changes the allowance. Changing the repair settings (the allowance or its start time) lapses the allowance of moves made earlier. Other onboarding edits, like listing the next pilot ticket, don't.

## Before turning it on

1. The independent review is configured on the service (`--review-dispatcher` and `--review-model`, see docs/review.md). Repairs read its recorded verdicts; while the service runs the stand-in reviewer, nothing ever reports a failure, so no repair starts. A second repair also needs the review's `extra_passes`, because by default a task gets one full review and one verification pass.
2. The routine's saved prompt is replaced with the current `controller/adapter/routine_prompt.md`. The old prompt refuses a task message with a `repair` part, so a repair would stop at the worker.
3. The live demonstrations in ENG-163: one successful repair, and one capped repair that ends with the report.
4. Rolando sets `repair_allowance` and `repair_allowance_since` in the onboarding file.

## Limits

- The factory never stops a running worker and never clears one itself.
- Native auto-fix stays off. ADR 0001 §7 and the governance map (G-C2, G-C9) still describe every repair as typed. Reconciling them is ENG-163's job.
- A repair go-ahead is a factory action under Rolando's allowance, not a decision he made. The record says so.

## An accepted unknown writer stays a manual exception

Accepting the risk that a lost worker might still exist does not establish
that it finished. Automatic repair checks therefore ignore
`unresolved-accepted` clearings and require qualified writer clearing across
all tasks in the shared lane. This is checked before asking for a repair
permission and again inside the existing locked launch reservation, including
when a signed repair permission was already saved before a restart.

A later completed/terminated clearing covering the known sessions, or verified
removal of write access, can release the automatic repair. Rolando can still
explicitly authorize a particular repair through the existing typed flow;
that manual decision takes precedence over a saved automatic permission and
retains the existing exception rules. The automatic path never makes that
decision for him.
