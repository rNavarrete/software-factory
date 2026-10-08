# What the factory says on Linear tickets (ENG-178)

The factory now reports on the Linear ticket itself. Rolando sees where each ticket's work stands, answers product questions and records observations there, without opening a terminal or a thread. The code is in `controller/report/`.

Nothing written in a Linear comment can approve anything. A contract, a repair, a clearing or a release still needs its signed record. A reply only answers a product question, records an observation on one exact commit, or logs time.

## The ticket is the board

Every entry the factory posts opens with a bold stage:

| Stage | When |
|---|---|
| Queued | The Todo move was accepted |
| Waiting | It can't start yet; the entry says what it is waiting for (approval, a cap, a hold, a provider) |
| Working | The worker started |
| Reviewing | The worker's PR is up and the independent review started |
| Repairing | An automatic repair actually started (posted only after the worker launched). Before that, a Waiting entry says the fix is eligible and how many repairs are left |
| Needs your decision | A product question, an observation request, or a suggested repair |
| Ready for your review | What changed, what was checked, the limitations and the exact reviewed commit |
| Failed / Stopped | The work ended, and why |
| Unclear | It is not known whether a worker started. The lane stays held; nothing restarts on its own |
| Merged / Merged as an exception | The merge record (below) |
| Released | The release record, kept separate from the merge |
| Notice | Pause and resume, a restart after downtime with the pending work, a provider switch, time |

The newest entry is where the work stands. Each entry is posted once, on a change; there are no routine check-ins.

The factory does not change the team's states or add labels. It must not overwrite a state Rolando set, and intake (ENG-174) pins a ticket's labels as part of the text he approved, so a label added by the factory would cancel his own Todo move. The Engineering team has no "In Review" state either. The stage in the comments stands in for both.

## Each message is posted once

Every message has a unique key in the service's outbox. The reporter turns the key into the comment's id, so the same message always has the same id. Before creating a comment it asks Linear whether that id exists. If it does, an earlier try worked and nothing more is posted. A restart, a crash between posting and saving, a timeout or a lost answer therefore never shows the same message twice. Once a message is recorded as posted it is never posted again, even if someone deletes the comment.

When Linear is down or rate limiting the factory, the reporter says so and the service stops posting for that round. The messages wait in the ledger and go out later, with growing waits between tries. Retrying a message never repeats a launch. A ticket's comments always go out in the order they were written. While an older one waits to be retried, newer ones for the same ticket wait behind it, so the newest comment is always the current stage.

The factory posts as its own Linear user. Whose key it is gets checked for each key value before that key is used, so replacing the secret takes effect at once and can't skip the check. If the key acts as Rolando, nothing is posted, and the reason is recorded every round, because a comment that looked like his could pass for his decision.

## What goes into a comment

Text from outside the factory (ticket text, model output, GitHub, error messages) is cleaned first. Anything that looks like a token or password is redacted. Invisible and text-direction characters are removed. Markdown in it is escaped, so it can't add links, images or a heading that looks like a factory stage. The factory's own link fields (pull request, preview, diff) keep only plain `https` addresses. Long text is shortened. One gap: Linear may still turn a bare web address in outside text into a link.

## Product questions

A question that comes from drafting the contract (ENG-175) is posted as one comment with the question, why it matters, the options with what each one does, the factory's recommendation (marked as only a suggestion), and what happens if nobody acts. The same question on the same ticket text is posted once. If the ticket comes back to Todo unchanged, a short note says the answer isn't in the ticket yet, instead of going silent. When the factory can't draft for its own reasons, or the ticket changed after the move, the comment is a plain notice with nothing to choose.

For now Rolando answers by putting his decision into the ticket's text and moving it to Todo again. The contract is drafted only from ticket text he moved, so a comment never becomes part of the work on its own. The recommendation is never taken as his answer, and silence never is either.

Replies in the question's thread are already read safely (`DecisionReader.answers`, `controller/report/replies.py`): only Rolando's own user, with no bot, app or synced integration involved, and never edited. Anything else is ignored with the reason. A reply by someone else, a copied approval and a bot comment never count. Using those replies in drafting is a later step.

## Observations and time

- **Observations.** The "Ready for your review" entry, and any request for a look, name one exact commit. A reply in that thread starting with `Observation:` is recorded against that commit only. A later change needs a new look.
- **Time.** A comment `time: 15m` or `time: 1h` by Rolando adds to his entered time on the ticket. The factory keeps entered time and measured time apart and says "unavailable" for what it doesn't have. It can't see his screen, so measured time is usually unavailable.

## Merge and release records

A merge is recorded as fully verified only when the independent review passed on the exact commit that was merged. Otherwise it is recorded as "Merged as an exception", listing what is missing: no review, no verdict yet, a review of a different commit, a failed review, or open findings. Until automatic review (ENG-156) returns verdicts to the service, every merge is recorded as an exception that says the review has no verdict yet. A release is always its own record, naming the release, the commit and the approval record.

## Before switching it on

1. The factory has its own Linear identity, and its key is set as `linear-key` (the same one intake uses).
2. Run this once from any folder on the Mac. It posts one test comment on this ticket, tries it twice more, and checks that exactly one comment shows, by the factory's own user with no bot attached, and that its marker lines came back unchanged:

   ```sh
   fly ssh console --app rnavarrete-factory -C "sh -c 'cd /app && FACTORY_SECRETS_DIR=/run/factory-secrets python3 -m controller.report probe ENG-178'"
   ```

3. Run the service in live mode (`FACTORY_MODE=live`, see `docs/service.md`). Live mode posts with this reporter.

These belong to the qualification run in ENG-163.

## Not built yet

- **Asking questions while the worker runs.** The service only asks a question while drafting. The independent review (ENG-156, docs/review.md) now posts the ready-for-review entry, observation requests and the merge record's review evidence.
- **Repair, provider-switch and release entries.** The texts are ready, but the service doesn't yet hold repairs (ENG-160), a second provider or release records to fill them.
- **Pause and resume from Linear.** Intake (ENG-174) reads them; this posts the acknowledgement. Recovery and escalation still run over `fly ssh console`, because clearing an unclear worker needs a signed record that a comment can't give.
- **Ignored replies are returned, not yet posted.** `LinearDecisions.read` returns why each reply was ignored; posting that on the ticket is left to whoever wires the reader into drafting.

## Known limits

- **Linear's comment ids.** The reporter relies on Linear accepting a comment id chosen by the client, as its API documents for create inputs. The probe above checks it. If Linear ever stopped accepting it, every post would fail visibly rather than repeat.
- **A message that can never post** (its ticket was deleted) is retried at most once an hour, for good. It costs one request an hour and is recorded each time. Because a ticket's comments keep their order, the newer messages for that ticket wait behind it.
- **Comments past the 2,000th on one ticket are not read.**
