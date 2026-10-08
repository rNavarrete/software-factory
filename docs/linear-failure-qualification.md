# Linear-to-PR failure qualification

[ENG-158](https://linear.app/rolando-projects/issue/ENG-158) supplies this checklist to
[ENG-163](https://linear.app/rolando-projects/issue/ENG-163), the single pilot go/no-go.

**Status: offline evidence recorded; connected live qualification remains open.**
This is not permission to enable intake, repairs, a reviewer or additional workers.
The initial pilot has one implementation lane. Provider pools and parallel tasks
need their own incremental qualification, not extra prerequisites for this pilot.

## Revision and results

- Controller base: `df1c598d501c606697399fe15f871e8443f6700c` plus this ENG-158 change.
  Use the containing PR's tested head SHA to identify the complete patch.
- Configuration: existing synthetic GitHub and Linear fixtures; SQLite and real
  controller components where those tests use them. No production configuration
  was changed. Tests called no real worker service or OpenAI model.
- New regression: four cases incorrectly reported **Merged** after a saved pass:
  destination branch changed, contract marker removed, base commit changed, or
  fresh evidence unreadable. All four failed before the fix. Merge closeout now
  runs the existing review check before counting a merge as verified. A changed
  head remains an exception; an unchanged reviewed merge remains verified after
  reviewer restart. Six tests cover this boundary through the real workflow
  result reader, reviewer, ledger, service closeout and Linear reporter.
- Real launches during this qualification: **0 workers, 0 reviewers**.
- This does not qualify model judgement, deployed permissions or the full hosted
  service. API fixtures cannot establish those properties.

## Failure checklist

Each row names existing executable evidence, not another testing framework.
Run the full suite below; these tests cover different boundaries and should not
be presented as one complete live end-to-end run.

| Failure that must be refused | Deterministic evidence | Qualification limit |
|---|---|---|
| Replayed/out-of-order Todo events, bot transitions, edited scope, duplicate delivery | `test_intake_service.py`; `test_todo_move_approval.py` (`AuthorizerTests`, `ChangedWhileQueuedTests`, `RealDispatcherTests`) | Real source/authorizer/dispatcher behavior with fake Linear; deployed authenticated move still needs observation. |
| Restart causes another launch, unknown launch permits a replacement, a live writer gets an automatic repair | `test_service.py` (`SqliteServiceTests`); `test_recovery.py`; `test_repair_writer_clearance.py`; `test_repair.py` (`RepairServiceTests`) | Includes lost reply, restart after signing/recording and locked reservation races. Does not prove the provider has stopped writing. |
| Attempt/repair/weekly caps reset after restart or ticket toggles | `test_attempts.py`; `test_ledger_attempts.py`; `test_repair_allowance.py`; `test_repair.py`; `test_intake_service.py` | Signed allowance, persisted counters and state toggles tested. Paid service usage requires its own live reading. |
| Missing/failed/stale/spoofed CI or independent review counts as success | `test_collect.py`; `test_review_workflow.py`; `test_review_auto.py`; `test_redteam.py` | Workflow actor, source, revision, artifact and request binding are tested with synthetic API answers. Actual protected workflow configuration remains a live boundary. |
| Weakened assertions, protected changes or untrusted text bypass human authority | `test_verify_assertions.py`; `test_verify_criteria.py`; `test_prepare_hostile.py`; `test_report_replies.py`; `test_todo_move_approval.py`; `test_repair.py` | Evidence parsing, signatures and protected-change routing are code checks; worker credentials and release permission are platform checks. |
| New push/base/destination/state leaves stale review or observation usable | `test_review_auto.py` (`StaleCommitTests`, `SupersededEvidenceTests`); `test_review_workflow.py` (`RevisionAndRestartTests`); `test_report_replies.py`; `test_merge_qualification.py` | Checks evidence validity. Historical Linear comments remain historical reports; this does not certify delivery-time freshness of queued comments. |
| Human merges without a still-applicable passing review | `test_report_service.py` (`ReviewServiceTests`); `test_merge_qualification.py` | Report an exception, never prevent Rolando merging. Unreadable fresh evidence conservatively counts as an exception. |
| Outage/restart loses or duplicates progress reports; service acts as Rolando | `test_report_service.py` (`OutboxTests`); `test_service_live.py`; `test_report_reporter.py` | Local HTTP/Linear fixtures exercise transport and identity checks; deployed restart is still needed. |

## What still needs live evidence

Keep this list with the ENG-163 record; do not repeat established probes just to
increase a test count.

1. **Connected deployed path:** an authenticated Todo move starts one worker;
   repeat polling and an unattended restart start no replacement. Capture the
   issue/event, controller and pilot SHAs, onboarding/policy versions, session,
   dispatch count and resulting PR. Include a meaningful product-question flow.
2. **Protected Codex review and bounded repair:** observe actual review provenance,
   one permitted repair after the old writer is cleared, and a verification pass
   on the changed commit. The real review-to-repair connection is PR #37, separate
   from this patch; rerun qualification against the revision that includes it.
   Record all worker and reviewer launches, including failed/unknown responses.
3. **Authority and release boundaries:** reuse dated ENG-142/143/180 evidence only
   after confirming the installation, collaborators, ruleset, workflow permissions,
   signer boundary and release environment still match it. The old
   [control audit](control-audit.md) and `controller/audit/controls.py` index those
   records. Their `planning/...` source files are absent from this checkout, so
   this pass has not independently revalidated them. Retrieve the originals;
   re-probe only changed or unsupported boundaries. A Done ticket alone is not
   evidence of the current configuration.
4. **Current failure reporting:** use the connected sample to observe stale-review
   explanations, a changed candidate and merge-before-ready handling. Include a
   delayed Linear delivery after a new push; the offline suites above do not
   establish that a queued ready comment is withdrawn before delivery.

For each live observation record expected vs observed behavior, exact revisions
and configuration, links to evidence, launch totals and actual manual steps.
ENG-158 stays In Progress until the necessary observations are recorded. ENG-163
makes the go/no-go decision; this document does not add a second approval gate.

## Reproduce the offline checks

```sh
python3 -m unittest tests.test_merge_qualification
python3 -m unittest discover -s tests -t .
python3 -m ruff check .
python3 -m ruff format --check .
```

Use Linux CI for the complete signer/socket qualification. The signer relies on
Linux `SO_PEERCRED`; macOS skips those tests. The live-service fixture now has the
same platform guard as the other signer fixtures, instead of hanging on macOS.
The CI matrix runs Python 3.11 and 3.13 on Ubuntu with this guard inactive.
