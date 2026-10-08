# Recovering an interrupted run

The controller cannot read or stop a cloud session, and the start call has no way to say "only once". So when a run is interrupted, the controller never tries again on its own. It stops, tells you what it knows, and waits for you to record what you found. This page is what you do. The rules come from ADR 0002 sections 6 and 6.1; `stop-procedure.md` in `controller/attempts/` covers stopping a worker.

`factory status` and `factory reconcile` are the subcommands ADR 0002 names; the dispatch work (ENG-176) wires them and the other steps to the methods of `Recovery` in `recovery.py`, named in brackets below.

## What the controller does by itself

- **At every start** it runs recovery (`recover`). A launch whose start was recorded but whose answer never was (the controller crashed, the Mac slept, the power went) is marked *launch-outcome-unknown* once it is ten minutes old. It is never fired again. A younger one is left alone, because the call may still be in flight.
- **On every dispatch** it refuses while any earlier attempt might still have a session writing to GitHub. That holds across restarts: everything is read back from the ledger.
- **`factory status`** (`status`) shows each attempt's state (below) and, separately, whether a session may still be writing.
- **`factory reconcile <attempt>`** (`reconcile`) reads the attempt's branch and PRs from GitHub and records what is there. If the worker opened a PR and the controller never saved it, this finds and records that PR. It never opens one. It also lists any PR that names the attempt but doesn't match it (wrong branch, wrong title marker, wrong author, a fork).

## States

| State | Means |
|---|---|
| ready | No attempt yet |
| dispatching | The start call is in flight |
| launch-outcome-unknown | No usable answer. A session may exist |
| running | A session started; no PR yet |
| verifying | The attempt's PR is open and its checks aren't all in |
| awaiting-human | You decide: review, repair, re-fire, or close |
| failed / canceled | You closed the attempt |
| accepted-merged | You merged its PR |

A draft PR, a PR with failing checks, an open PR with passing checks, and a PR closed without merging are all unfinished. Only a merge into `main` of the pilot repo counts as success; a PR merged into any other branch is reported as not the attempt's. A closed PR waits for you to say whether the attempt failed or was canceled. Release status is recorded separately: a merge is not a release.

## When a launch is unknown

1. Run `factory reconcile <attempt>`. Note any PR or branch it finds. A session URL inside a PR body is shown as a hint only, since the worker wrote that text.
2. Open the routine's run list on claude.ai and look for the session (or sessions) started at that time.
3. Record what you found (`record_launch_finding`): the session URL, every URL if there were several, or "not found". Say how you checked. "Not found" is kept as what you looked at. It never counts as proof that nothing started, because the start call may still create its session after you look.

## Freeing the lane: the clearing record

Nothing else starts until the attempt has a clearing record (`clear`). You type a code to confirm it and it is signed with your approval key. The controller refuses one that doesn't meet these rules:

- **completed** or **terminated**: you list every session on record for that launch, each seen finished, or stopped and archived. Leaving out a duplicate is refused.
- **write-access-removed**: you removed the bot as a collaborator on the pilot repo and say how you checked. The controller reads GitHub at that moment and refuses if the bot still has push access. Pausing or deleting the routine does not count.
- **unresolved-accepted**: your recorded exception for a session you searched for and never found. Refused while any session URL is on record, because that session can be checked instead.

A PR appearing, a quiet branch, hours without pushes, or the 45 and 90 minute run alerts never clear anything.

If a clearing is ever written some other way and doesn't meet these rules, or a session turns up after you cleared, dispatch refuses again (`clearing-invalid`) until you record a valid one.

## Closing an attempt

Closing an attempt as failed or canceled, with your reason (`close_attempt`), ends the work. It does not end the session: if one may still be writing, it still needs a clearing record.

## Giving the bot its access back

After a write-access-removed clearing, give the bot its push access back only when the controller says every attempt that may have started a session has a completed or terminated record (`Recovery.may_restore_bot_access`). Otherwise a surviving old session would get its access back too.
