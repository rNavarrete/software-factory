# Starting work from a Todo move in Linear (ENG-174)

Rolando starts factory work by moving a ticket to Todo in an onboarded Linear project. This page explains how the factory proves the move was his, which ticket text it approved, and what happens when things change afterwards.

## How a move is proved

The factory does not trust a ticket just because it is in Todo. Every 60 seconds the service reads Linear's own change history for the onboarded projects, over the GraphQL API with the factory's own Linear key (`controller/intake/linear.py`). A move counts only when Linear records all of the following:

- The entry changed the state from another state into Todo, after the project was onboarded (`intake_since`).
- It was made by Rolando's Linear user (`approver_linear_user_id`), and that user is not an app account.
- It has no bot (`botActor`: integrations, the GitHub sync, apps acting for him), no Linear automation or agent (`workflowMetadata`, auto-close), and no import (`issueImport`).
- The ticket is still in Todo, and nothing that defines the work (title, description, labels, project, team, parent) changed after the move.
- The move is at least two minutes old, so a quick edit Linear hasn't written to the history yet is still caught.
- The project's onboarding entry allows that ticket (see "Baseline tickets").

The exact ticket text is pinned as a `revision`: the sha256 of the title, description, labels, project, team and parent. The move's history entry id is its `event_id`.

Anything else is refused once, with the reason posted on the ticket. That covers a move by another person, a bot or an automation, an import, and an edit after the move. A ticket created straight into Todo, or imported there, is also refused once, and the comment says to move it to Backlog and back.

Before reading anything, intake checks whose key it is using. If the factory's Linear key acts as Rolando, intake reads nothing and says so every round, because the factory's own changes would then be recorded as his. The factory therefore needs its own Linear identity (an app, or a separate member) before intake is switched on.

## Why polling and not webhooks

Linear's webhooks would need a public address on the machine, and their payloads would need their own checks. Polling the history uses the one path the service already has: an outbound call every round, with a cursor saved in the ledger. Each poll reaches 15 minutes back before its cursor. That overlap is harmless, because a move that was already recorded is ignored by its `event_id`.

## When things change

| What happens | Before the worker starts | After the worker starts |
|---|---|---|
| A replayed or overlapping poll | Ignored: same `event_id` | Ignored |
| Moved out of Todo | The queued item closes with a note | Nothing more starts for it, repairs included. The factory says it can't stop a running worker, explains how to stop it from the routine's run page, and keeps checking GitHub |
| Text edited after the move | The item closes. Moving the ticket out of Todo and back approves the new text | The worker keeps its contract and nothing new starts. One note on the ticket says what changed |
| Moved out and back into Todo | The newer move replaces the queued one | No second worker. One ticket gets one attempt budget |
| Blocked by an open ticket | It waits and says what it is waiting for | — |
| The service restarts or Linear is down | The poll is read again from the saved cursor. Nothing is lost or doubled | Reconcile continues; the Linear check waits |

The service checks the move again right before every dispatch try, and again every five minutes while the worker runs.

## Baseline tickets

The pilot's baseline tickets (ENG-186, 190, 192, 193) are in the same Linear project as the factory tickets. The pilot's onboarding file (`deploy/pilot/onboarding.json`) therefore lists exactly the factory tickets in `issues` (ENG-187, 188, 189, 191). A Todo move on any other ticket in that project is refused with a note. `skip_labels` (`baseline`) is a second guard, should Rolando add that label to the baseline tickets. The file ships with `intake_enabled: false`.

## Who can sign what on the machine

The approval key is HMAC, so whoever can check a signature with it can also make one. On the machine it lives only in the signer (`controller/signer`):

- At start-up the signer reads the key as root, opens its socket for the service's user, and then becomes `factory-signer` for good.
- The service runs as `factory`. It can't read the key file or the signer's memory. It holds a `SignerKey`, which can ask "is this signature good?" and gets yes or no. It can never make a signature. The kernel tells the signer which user is asking, and only the service and root get an answer.
- Rolando's own commands over `fly ssh console` run as root and sign with the key file directly, exactly as before.

So nothing that reads ticket text, model output or GitHub data can forge Rolando's approval, a repair go-ahead or a clearing.

## Not built yet: a Todo move as the approval itself

The ticket asks for a verified Todo move to replace the typed approval code for an in-policy ticket. That changes the approval rules, so it waits for Rolando's go. The proposed design:

- The signer gains one more request: "authorize this contract for this Todo move".
- Before answering, the signer reads the move from Linear itself, with the same rules as above. It also checks that the contract is for that ticket and stays inside the project's onboarding entry: repository, routine, actions, checks and attempt budget.
- It then signs a record of its own kind, `source-authorization`, that names the move, the ticket revision, the routine and the contract digest. It is never a typed `human-decision`, so it can't pass for one.
- The record lasts at most one hour and covers attempt 1 only. It counts only for the service's routine. Repairs, re-fires and clearings still need Rolando's typed records.
- Each Todo move can authorize only one contract.

Until then, a ticket accepted from Linear waits with "Waiting before starting: no approval" until Rolando approves its contract over `fly ssh console`.

## Before switching intake on

1. The factory has its own Linear identity, and its key is set as `linear-key`.
2. Run this once from any folder on the Mac:

   ```sh
   fly ssh console --app rnavarrete-factory -C "sh -c 'cd /app && FACTORY_SECRETS_DIR=/run/factory-secrets python3 -m controller.intake probe ENG-174 cd9ec650-f957-4f25-b5f0-9c14bcae49c8'"
   ```

   It reads the history of a ticket a Claude session has changed through the Linear connector, and should show those changes carrying a bot, so they never count as Rolando's. It writes nothing.
3. Set `intake_since` to the time of switching on, and set `intake_enabled: true`.

These belong to the qualification run in ENG-163.

## Known limits

- **Linear's own record is the proof.** If Linear ever recorded an integration's change as Rolando's own with no bot attached, intake would count it. The probe in step 2 checks this for the connectors in use. Intake fails closed whenever Linear doesn't say who acted.
- **Edits Linear writes to the history late.** The two-minute wait and the check before every dispatch catch an edit that shows up in the history later. An edit that never shows up in the history at all is still caught before launch, because the text no longer matches its `revision`.
- **A running worker can't be stopped from here.** Moving a ticket out of Todo stops everything that comes after. The session itself has to be stopped on the routine's run page.
