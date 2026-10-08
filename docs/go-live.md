# Going live and the qualification run (ENG-163)

This page covers switching the factory from its practice run to real work on the pilot project, and the one live run that qualifies it. One command does the switch and walks the first ticket through. Everything after that runs by itself.

## What changes at the switch

- **Settings** (`deploy/pilot/onboarding.json`): intake is on from `intake_since` (2026-10-08 23:30 UTC). Only the listed tickets can start. One automatic repair is allowed inside two attempts (`repair_allowance` 1, `max_attempts` 2), for Todo moves made after that time. Notices that concern the whole project go to the status ticket, "Factory status and pause".
- **Secrets**: the factory's own Linear key and the routine's start key are added to Fly. Both go in without being shown.
- **Mode**: `FACTORY_MODE=live`. The service reads Todo moves from Linear, drafts contracts, starts real workers, starts Codex reviews and posts on the tickets.
- **Record**: the live service keeps its own ledger in `/data/factory`. The practice run's ledger stays in `/data/qualification`, untouched, with its fake ticket and its long-running alert. Nothing from it carries over, and `/app/factory` follows the running service's home.

## The command

From the root of the software-factory checkout on the Mac, on `main`, after this change is merged and CI on `main` is green:

```
sh deploy/fly/go-live.sh
```

It refuses to run on another branch, on an out-of-date `main`, with local changes, or while CI on `main` is red. It is safe to run again: each finished step is skipped, and an interrupted run picks up where it stopped. It stops only where Rolando must act:

1. **The factory's own Linear user and key.** A Member invited by email, with a personal API key limited to "Read" and "Create comments". With those permissions the factory can't change a ticket's state, labels or text even if its code tried. The script checks with Linear that the key isn't Rolando's before storing it.
2. **The routine's saved instructions.** The current text (which accepts a repair) is put on the clipboard; he pastes it over the old text in the routine on the factory account. The script records which version was pasted on the machine, so it asks only once per version.
3. **A usage reading** from the factory account's usage page. Workers don't start without one.
4. **Linear actions.** Moving the sample ticket to Todo, answering its question, then moving the edit-check ticket to Todo and editing it.

Between those it deploys once in practice mode with the new keys, runs the two read and post checks (`controller.intake probe` on a ticket only the Linear connector has changed, and `controller.report probe`), and switches to live only if both pass. If a check fails, the factory stays in practice mode.

## The live run

The run uses three new, ordinary tickets in the pilot's Linear project, so the eight pilot tickets stay untouched:

| Ticket | What happens | What it shows | Worker starts |
|---|---|---|---|
| "Show how many books are on the list" | Moved to Todo. One criterion is still an open question, so the factory asks it on the ticket. Rolando answers by editing that line and moving the ticket out of Todo and back. | The product-question flow, then a real Claude implementation from the answered ticket | 1 |
| (same) | After the worker starts, the script restarts the machine and watches two rounds. | A restart starts nothing twice | 0 |
| (same) | The Codex review starts on its own when the draft PR appears. | An authenticated review bound to the exact commit, base and contract | 0 (1 review) |
| (same) | If the review fails it: after Rolando confirms the first worker finished (the ticket gives the exact command), one repair starts on its own, and its PR gets a fresh review. If that fails too, the budget is spent and the ticket says so. | One bounded repair and a fresh review after a correction; the cap | 0 or 1 (0 or 1 review) |
| "Qualification check: edited after the Todo move" | Moved to Todo while the first worker holds the lane, then edited. | An edit after the move withdraws the task before it starts | 0 |
| "Qualification check: a Todo move made by an app" | Moved to Todo through the Linear connector (by Claude, acting for Rolando) while the first worker holds the lane. | Linear records it as an app's move and the factory refuses it | 0 |

At most two worker starts and two review runs, against a weekly cap of 12. The two check tickets only run while the first worker holds the one lane, so even a wrong acceptance couldn't start a second worker before it was caught; if one were queued, moving that ticket out of Todo closes it.

If the first review passes, no repair happens. The run then shows the review and the merge record but not a live repair; whether to spend another ticket on that is Rolando's call.

Things to watch that offline tests can't settle:

- **Linear's GitHub sync.** If the workspace's GitHub integration moved a ticket to In Progress when its PR opened, the Todo move would stop counting and the repair would be refused (the rehearsal test covers what the factory does then). No PR in either repository has been auto-linked so far, which suggests the integration isn't connected.
- **The routine prompt change** is a new prompt revision. The live worker start doubles as its smoke run.

## Offline rehearsal

- `tests/test_go_live_rehearsal.py`: the shipped settings, the three ticket texts through the real drafting code (the open question is asked, the answered ticket becomes a tested task), and the run's order of events on the offline harness with the pilot's terms: one start, a restart that starts nothing, a failed review, the clearing, one repair, a restart during it, the cap; a bot move and an edit while the lane is busy; the ticket leaving Todo after the start; the empty live ledger.
- `tests/test_go_live_script.py`: the command against stand-ins for Fly, GitHub, Linear and Keychain: a full first run, a re-run that does nothing twice, secrets never on a command line or in output, Rolando's own Linear key refused, wrong branch, stale `main`, local changes, red CI, a failed check staying in practice mode and then resuming, a connector change counted as his, a second worker after the restart, a worker for the edited ticket, a refused first ticket and an open hold.
- Earlier offline cases still hold: `python3 -m redteam` (the Linear path) and `python3 -m controller.audit`.

## Pause and recovery

- **Pause new work:** add the label `factory-pause` to the "Factory status and pause" ticket in Linear's own app; remove it to resume. Or from the terminal: `fly ssh console --app rnavarrete-factory --pty -C "/app/factory hold paused 'pausing from the terminal'"`.
- **A running worker** can't be stopped from the factory. Stop it on the routine's run page on the factory account.
- **Back to practice mode:** `fly secrets unset FACTORY_MODE --app rnavarrete-factory`. That restarts the machine on the practice ledger; the live ledger stays on the volume for when it is switched back.
- **Where things stand:** `fly ssh console --app rnavarrete-factory -C "/app/factory status"` (attempts, holds, the usage reading, worker starts) and `-C "/app/factory queue"` (each ticket the service took in and how it ended). Logs: `fly logs --app rnavarrete-factory --no-tail`.
- Everything else (an unclear worker start, a lost volume) is in [service.md](service.md).

## Results

The go/no-go record (tested revisions and settings, what happened, the manual steps it took and the remaining limits) is written after the run.
