# The factory's background service

The factory now runs on a small always-on machine instead of Rolando's laptop. The machine watches for work, starts one worker at a time through the same checks the terminal commands use, and keeps the factory's record. Rolando's laptop can be closed or asleep.

This is the foundation from ENG-194. Reading Todo moves from Linear (ENG-174), drafting the task contract (ENG-175) and posting progress in Linear (ENG-178) are wired in and run together in live mode (below). Automatic review (ENG-156) and repairs (ENG-160) still use fixtures. For now the machine still runs a qualification fixture with a fake worker, so it never starts a real worker.

## Where it runs

| | |
|---|---|
| Host | Fly.io, one `shared-cpu-1x` machine with 256 MB of memory and a 1 GB volume, region `iad`, app `rnavarrete-factory` |
| Monthly cost | About $2.10: the machine is about $1.94 and the volume $0.15. Volume snapshots are free up to 10 GB. Prices from fly.io/docs/about/pricing, October 2026 |
| Owner | Rolando: the Fly account, its billing, and the secrets |
| Files | Dockerfile, start script and `fly.toml` in `deploy/fly/`. The qualification fixture is in `deploy/qualification/` |
| Code | `controller/service/` (`python3 -m controller.service`) |

Rolando chose Fly.io on 2026-10-08 over Render (about $7 a month), a Mac mini at home, and a rented Linux server. Fly was the cheapest, has nothing to patch, and can only run one copy (see below).

## What lives where

Everything is on the machine's volume under `$HOME/.software-factory/`, which is the same folder the `python3 -m controller` commands use:

- `ledger.db`: the factory's one record. It holds the attempts and launches as before, plus the service's queue, intake cursor and outgoing Linear messages, all stored as ledger events (`controller/service/queue.py`). The service keeps no state anywhere else, so a restarted service rebuilds exactly what the old one knew.
- `backups/`: a copy of the ledger after every round that wrote something. The newest 48 are kept.
- `contracts/`: each contract the service drafted, stored read-only and named by its digest.
- `service.heartbeat`: when the last round finished.
- `service.lock`: held while the service runs.

The worker never sees any of this. It runs in Anthropic's cloud and receives only the fire text, which holds the contract and the markers. It has no way to reach the machine.

For now, `HOME` is `/data/qualification`, so the qualification run's fake worker has its own ledger and never counts against the real fire limits. Live mode uses `/data/factory`. The start script records which home is in use in `/run/factory-home`, and `/app/factory` reads it, so Rolando's commands over `fly ssh console` always act on the running service's ledger.

## Live mode

`FACTORY_MODE` decides what the start script runs. When it is unset, the script runs the qualification fixture, as it always has. With `FACTORY_MODE=live` it runs `python3 -m controller.service run --live --real-runtime` with the pilot's onboarding file (`deploy/pilot/onboarding.json`). That means:

- Todo moves come from Linear (ENG-174), and the signer turns Rolando's verified moves into approvals.
- Contracts are drafted from the ticket text with `deploy/pilot/drafting.json`, from the newest main commit that passed CI (ENG-175).
- Progress, questions and refusals are posted on the tickets as the factory's own Linear user (ENG-178).

The pilot's onboarding file ships with `intake_enabled: false`, so the live service refuses every Todo move, says so on the ticket, and starts nothing until that file says otherwise.

Before its first round, the live service checks what it needs. It does not start, and says why, if any of these is missing or wrong:

- the `linear-key` or `github-token` secret;
- the signer, which must actually answer on its socket;
- the onboarding file, which must be readable and name the approver (the same one as `--approver-linear-id`) and the time intake reads from;
- the drafting policy, which needs an entry with the same base branch for every onboarded project.

These checks ask only local files and the signer, never Linear or GitHub.

While it runs, the factory's Linear key is checked before every request against everyone it must never act as: Rolando, and whoever the onboarding file has named as approver since the service started. If the file is changed to name another user, that user's key stops working for posting too, not just for intake.

Switching it on belongs to the qualification run (ENG-163), after the factory has its own Linear login and the probe in `docs/reporting.md` has passed.

## Secrets

There are four secrets. Each is scoped to one job, and none is a copy of Rolando's own credentials:

| Name | What it can do | Made where |
|---|---|---|
| `approval-key` | Signs and checks decision records in this machine's ledger | Generated straight into Fly's secret store. It is a new key, not the Mac's Keychain key |
| `github-token` | Reads the pilot repo (contents, pull requests, metadata). It cannot write | A fine-grained GitHub token limited to that one repository |
| `routine-token` | Starts the factory routine, nothing else | The routine's own start key. Not needed until real workers start |
| `linear-key` | Reads Todo moves and ticket text, and posts the factory's comments (ENG-174, 175, 178) | The factory's own Linear login, never Rolando's. Needed only for live mode |

They are set with `fly secrets`. Fly encrypts them and they can't be read back from the command line. When the machine starts, `deploy/fly/entrypoint.sh` moves them out of the environment into files at `/run/factory-secrets/`. The folder is mode 0700 and each file is mode 0400. Child processes therefore never inherit them. `FileSecrets` refuses any file that is a symlink, belongs to another user, or can be read by group or others (`controller/service/secrets.py`). The ledger redacts anything that looks like a token, and no secret is ever logged.

On the machine, the controller commands read the same files whenever `FACTORY_SECRETS_DIR` is set. Rolando still confirms every decision by typing its code, now in a `fly ssh console` session. His Fly login takes the place of the Keychain prompt as proof that it is him.

## Only one copy runs

Three things together stop two copies from running:

1. A Fly volume attaches to exactly one machine, and the app is deployed with `--ha=false`.
2. The service holds `service.lock` for its whole life. A second copy on the same machine exits with status 3.
3. The ledger's writer lock and the attempt gate still decide every fire. A repeated dispatch of the same contract only reports the attempt already on record.

## What one round does

Every 60 seconds (`controller/service/service.py`):

1. Re-read the onboarding file (below). If it changed, record its sha256.
2. Recovery. A fire left without an answer by a copy that died mid-launch is marked "unclear whether it started" and is never fired again.
3. Intake. Read Linear from the saved cursor, apply pause and resume, then accept or refuse each verified Todo move. The results and the new cursor are saved in one write, so a crash re-reads the batch instead of losing it. A move that was already seen is ignored.
4. Work, oldest ticket first. For a running attempt, check GitHub, start the review once its PR appears, and close the ticket when it is merged or finished. Otherwise, draft the contract, check it against the onboarding entry, and dispatch it through `Dispatcher.dispatch`, which applies the approval check, the attempt gate (one worker at a time, caps, holds, rate-limit waits) and recovery. At most one fire happens per round.
5. Post the queued Linear messages. A failed post is retried later with growing waits. If Linear is down or rate limiting, posting stops for the round. Retrying a message never repeats a launch.
6. Write the heartbeat, and back up the ledger if it changed since the last backup that worked. A failed backup is tried again every round until it works.

Each step stands alone. If Linear or GitHub is down, only that step waits for the next round.

The service never approves, merges or releases anything, and it never signs a decision. It fires only a contract Rolando has approved: by typing the code, or, with `--source linear`, by his own Todo move as checked and signed by the signer process (docs/intake.md). Until then, a queued ticket shows "Waiting before starting" with the reason.

## Onboarding a Linear project

`onboarding.json` in the home folder maps each Linear project, chosen explicitly, to what the factory may do for it. `deploy/qualification/onboarding.json` is an example:

- `linear_project_id`, `name`
- `repository` and `routine_id`. In v1 these must be the pilot repo and the factory routine, and any other value refuses the whole file.
- `allowed_actions`, `checks` and `max_attempts`. These are the most a drafted contract may ask for. A contract that names another repository, another task, a new action, a new check or a bigger budget is refused before dispatch.
- `repair_allowance` (default zero) is how many of those attempts the factory may start on its own as repairs. A positive value needs `repair_allowance_since`, the time repairs were switched on; only later Todo moves get it. See [repair.md](repair.md) and [repair terms](intake.md#repair-allowance-recorded-with-the-move-eng-160).
- `status_issue_id`: a Linear ticket for notices that concern the whole factory, such as outages and pause.
- `intake_enabled` (top level). This is off unless set. While it is off, every Todo move is refused, and only work already queued continues. It stays off until the full path is qualified (ENG-163).

A Todo move on a project that is not listed is refused, and the refusal is posted on the ticket. If an entry is removed, tickets from that project that are queued but not started are closed at the next round, and nothing new starts.

## Pause and resume

From Linear (once ENG-174 reads the controls): Rolando's verified pause becomes a factory hold named `paused-from-linear`, and his resume lifts only that hold. The service confirms each one on the ticket or on the status ticket.

From the terminal, today:

```sh
fly ssh console --app rnavarrete-factory --pty -C "/app/factory hold paused 'pausing from the terminal'"
```

Pausing stops new workers only. It does not stop a worker that is already running, because there is no API to cancel a cloud session. To stop one, open the factory routine's run page on the factory claude.ai account and stop the session there.

## When it is unclear whether a worker started

The service posts this on the ticket: "It is unclear whether worker … started". It then holds the one-worker lane and never re-fires on its own. To settle it, look for the session on the factory routine's run page, then record what you found and clear it, following `controller/recovery/reconcile-procedure.md`. Run those commands as `/app/factory <command>` over `fly ssh console --app rnavarrete-factory --pty`.

## Outages and recovery

- **The service crashes or the machine restarts.** Fly restarts it. On the first round after a gap of more than 15 minutes in the heartbeat, it posts "The factory was not running from … to …" on every open ticket and on the status ticket, then catches up from its saved cursor. Nothing is started twice: recovery marks any fire that was in flight as unclear, and dispatch reports attempts already on record instead of starting them again.
- **A deploy.** Fly stops the old machine before starting the new one, because the volume can only be attached once. The service finishes its current round before it exits on SIGTERM. If it is killed mid-launch, recovery handles it as above.
- **Linear or GitHub unreachable.** That step is retried every round. Messages wait in the ledger until they post.
- **The volume is lost.** Restore the newest Fly snapshot with `fly volumes snapshots list --app rnavarrete-factory`, then `fly volumes create factory_data --snapshot-id <id>`. Snapshots are taken daily and kept for 14 days. Anything written after the last snapshot is lost. Because of that, reconcile every attempt that was running, and treat any launch from that window as unclear until it is checked.
- **While the service is down, nothing can post about it.** Fly does not alert by default. Not set up yet: an external check that emails Rolando if the heartbeat stops.

## The seam for the next tickets

`controller/service/seams.py` defines one protocol per ticket. Each has a fixture in `controller/service/fixtures.py`, so it can be built and tested without the others:

| Protocol | Ticket | Contract |
|---|---|---|
| `AuthorizationSource.poll(cursor)` / `.standing(authorization)` | ENG-174 (`controller/intake`, docs/intake.md) | Returns only Todo moves it has proved were made by Rolando on an exact ticket revision. Each has a stable `event_id`, so a replay is ignored. Also returns the moves it refused, and pause/resume controls. `standing` is checked again before every dispatch try and every five minutes while the worker runs. |
| `ContractPreparer.prepare(authorization, project)` | ENG-175 | Returns a contract with a plain summary the service posts once, or a question with its kind (product, split, scope, changed, factory). A question closes the item, and moving the ticket to Todo again starts a new one. Built in `controller/prepare/`, see docs/prepare.md. |
| `Reporter.post(issue_id, key, text)` | ENG-178 | Posts on Linear. Must be idempotent per `key`, because the service retries when it can't tell a failure from a lost answer. Built: `LinearReporter` (`--reporter linear`), see `docs/reporting.md`. |
| `DecisionReader.answers(issue_id)` | ENG-178 | Rolando's own, unedited replies to the factory's product questions, for later use in drafting. Never an approval. |
| `ReviewStarter.start(pr, key)` | ENG-156 | Called when an attempt's PR first appears. The request and its key are saved in the ledger before the call, and after a crash the same key is sent again, so the reviewer must treat a repeated key as the review it already started. Runs under the reviewer's own identity. |
| `FailureSource.failure(attempt)` | ENG-160 | The review's failed verdict for an attempt, as a `FailureReport` (PR, exact commit, findings), or None. Built: `controller/repair/review.py` reads the ENG-156 review's own records. |
| `RepairAuthorizer.use_repair_allowance(...)` | ENG-160 | The signer's repair go-ahead for one attempt within the Todo move's allowance. See [repair.md](repair.md). |

The worker itself sits behind the existing runtime boundary: `RuntimeAdapter` in `controller/interfaces.py`, which the service reaches only through the `adapter` factory `Dispatcher` takes. A worker pool (ENG-154) plugs in there. The first deployment keeps one worker at a time.

ENG-174 settled how a verified Todo move becomes the approval: Rolando chose it on 2026-10-08, and the signer process signs it after checking with Linear itself (docs/intake.md).

## Known limits

- **Only the signer holds the approval key (ENG-174).** The key is HMAC, so whoever can check a signature can also make one. On the machine it therefore lives only in a small signer process running as its own user (`factory-signer`). The service runs as `factory`, can't read the key file, and can only ask the signer whether a signature is good. Code that reads Linear text, model output or GitHub data can't forge an approval. Rolando's own commands over `fly ssh console` run as root and sign as before. If the signer or the service stops, the start script stops the other and exits, and Fly starts the machine again with both. See docs/intake.md.
- **Each ticket gets one task and one attempt budget for its whole life.** If a ticket the factory already worked on is moved to Todo again, the service says so and does nothing. New work needs a new ticket.
- **A repair waits for Rolando to confirm the earlier worker finished.** The factory can't read or stop a session, so the ticket note gives him the exact clearing command. After that the repair goes on by itself if the move's allowance covers it; otherwise his typed go-ahead starts it, and the queued ticket stays open for it. See [repair.md](repair.md).
- **The GitHub token expires.** Fine-grained tokens last at most a year. When it expires, the service can't read GitHub and says so every round. Renewing it is one `fly secrets import` (see the setup steps in the PR).
