# Stopping the factory

The controller can refuse to start new work. It cannot stop a cloud worker that is already running: there is no API for that (ADR 0002 section 4, governance map G-C9). So "stop" means three separate things, and each one has its own steps and its own proof. Doing one does not do the others.

## 1. Stop future launches

This one is enforced by the controller.

1. Run `factory hold "<reason>"`. Every dispatch now refuses until `factory resume "<note>"`.
2. If the controller itself might be the problem, also pause the routine on claude.ai, or regenerate its token. This kills the start key only. A worker that is already running keeps its GitHub access (ADR 0002 section 6.1).

**Proof:** `factory status` shows the hold. A dispatch attempt is refused with `hold`.

## 2. Stop monitoring

Nothing in the factory polls. Monitoring only happens when someone runs `factory status` or `factory reconcile`, or when a session is subscribed to a PR. To stop it, stop running those commands and unsubscribe any session watching the marker PR.

Stopping monitoring does **not** stop the worker, and it does not free the single lane. An attempt nobody has checked stays unresolved and keeps blocking every new launch.

**Proof:** none needed. This is just "stop looking". It changes nothing the worker can do.

## 3. Confirm the running worker has stopped

This is advisory. The controller only records what you saw.

1. Open the session URL from the ledger (`factory status` prints it), or the routine's run list if the launch outcome is unknown.
2. If the session is still running, stop it in the claude.ai UI, then archive it. Whether archiving stops a running worker is not yet proven, so check that it now shows as stopped.
3. Record what you found, and how you know, as a clearing record:
   - **completed** or **terminated**, with the session URL; or
   - **write-access-removed**: remove the bot as a collaborator on the pilot repo (or revoke its Claude GitHub App authorization), then confirm from outside the worker that it can no longer push (the collaborators page, or `gh api repos/<repo>/collaborators`). Record how you checked. Deleting or pausing the routine does **not** count.
   - **unresolved-accepted**: only as a recorded exception, when the session can never be found. Later work on that task is checked for pushes from it.

A PR appearing, a quiet branch, minutes without pushes, or a stop in the UI with no recorded session state do **not** prove the worker stopped. "Not seen" is never recorded as "did not happen".

**Restoring the bot's access** after a write-access-removed record: only once every unresolved attempt has a completed or terminated record. Restoring it earlier would also give back access to any old worker that is still running.

**Proof:** the clearing record in the ledger. Until it exists, the attempt counts as unresolved and the controller refuses every new launch.

## Run time alerts

At 45 minutes after a fire, `factory status` shows an alert. At 90 minutes it marks the run overdue. Both are reminders to do section 3. Neither stops anything, and an overdue run keeps holding the lane.
