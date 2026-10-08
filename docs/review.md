# The automatic independent review

When a worker opens a PR, the factory now starts an independent review of it by itself. This page says what the review does, what it can and can't do, and what has to be set up before it runs for real.

## What it does

Each round, the factory reads the PR again from GitHub (`controller/review/reviewer.py`):

1. **Can it be judged at all?** The PR must be open, aimed at the pilot's `main`, from the attempt's own branch, opened by the worker, with the approved contract's markers. Anything else stops here and is reported straight away, before any review job is started. A failed CI run or a wrong title or body marker is something a repair can fix, so it goes to the repair worker. Anything else (another repository or branch, a closed PR, an author that isn't the worker, CI evidence that can't be read) goes to Rolando.
2. **Which revision is it?** The contract digest, the PR's head commit, its base commit and the merge base, plus the PR number, make a request key. The same key always means the same review. It is launched at most once, and a verdict already recorded for it is reused. A new push or a moved base is a new key.
3. **Is CI finished?** While CI runs, the factory waits. When CI or a gate (scope, base, checks) has already failed, the verdict is "failed" at once, with findings for the repair worker. No review job is spent on it.
4. **Start one review job.** This is one fresh, scoped session, on the reviewer routine and as the reviewer's own GitHub account (`reviewer_prompt.md`). The claim is written to the ledger before the launch, so a crash or a second copy of the service never launches it twice. A launch whose answer was lost is never repeated: it is reported as "unclear whether it started", and a result is still read if one appears.
5. **Read the result.** The job posts one comment holding the criterion-to-assertion mapping, the failure proofs and any findings. The two existing verifiers (`verify/criteria.py`, `verify/assertions.py`) then decide. Only a comment from the configured reviewer account counts. Comments from the worker, from Rolando's account, from any other account or look-alike name, edited comments, and comments for another revision or contract are all ignored.

The verdict is one of these:

- **passed**
- **failed**: findings for the repair worker.
- **needs Rolando**: a behavior only he can check, a change to a protected control, or a product or security question.
- **unknown**: the job's launch was lost, it never posted, or the comment a verdict rested on was deleted.
- **blocked**: an allowance is used up, the factory is on hold, or the usage reading is high or old.
- **not reviewable**

Each verdict names the exact commit it covers. A passed verdict is evidence for Rolando, never an approval. The reviewer can't approve, merge or release, and nothing here records a decision.

## Findings

Every finding has these fields (`verify/findings.py`):

- **id**: stays the same when the same problem appears on a new commit.
- **severity**: blocking or advisory.
- **category**
- **route**: repair or Rolando.
- **summary** and **evidence**
- **suggested action**
- **commit**: the exact commit it was found on.
- **resolved**

When a repair pushes a new revision, the next review is a verification pass. It gets the open findings and checks them on the new commit. Findings the new revision no longer raises are marked resolved, once.

The reviewer's own finding ids are ignored. Ids come from what the finding says, so a reviewer can't mark someone else's finding resolved.

## Allowances

The defaults are one full review and one verification pass per task and approved contract. More passes need an explicit allowance (`ReviewPolicy.extra_passes`). The factory never loops until reviewers agree.

A job that was definitely not launched (for example, a missing start key) may be launched once more. A lost one is never launched again.

Review launches count in the same weekly fire allowance as worker launches (12 a week). They are also stopped by the same holds, the same 429 wait and the same usage reading. A launch that was rejected still counts.

## What must be set up before it runs for real

Until a reviewer account is configured, nothing is launched and the review reports "No reviewer account is set up".

1. **A reviewer GitHub account.** It must be a plain user account that is not the worker's bot and not Rolando's, and it must have no access to the pilot repository. It can still comment, because the repository is public. Check it with `python3 -m controller.review qualify-identity <login>`.
2. **A reviewer routine** with the saved prompt in `controller/review/reviewer_prompt.md`. It needs its own start key, kept in the service's secrets like the worker's. Its GitHub access must be the reviewer account. The cleanest option is a separate claude.ai account linked to the reviewer GitHub account. Running it on the factory account would post as the worker's bot, and those comments are ignored.
3. **Service wiring.** Today the service calls `start` once when it first sees an attempt's PR. It also needs to call `check` each round, and pass the latest `evidence` into the merge record (ENG-178's `ReviewEvidence`, whose `reviewed_commit` is the verdict's exact commit). This lands after the ENG-178 PR merges.

## Trying it without starting anything

```
python3 -m controller.review dry-run tasks/samples/clear-finished.json 15 --as-open
```

This reads the PR with `gh api` as you. It uses a throwaway ledger, sends nothing, and prints the request key, the verdict, any findings and how many jobs would have been claimed. It runs the check twice, to show that the same revision gives one job.

`--as-open` replays a merged sample PR as if it were still open. GitHub drops a CI run's link to its PR at merge, so the replay restores that link from the PR itself.

## Limits

- The verdict rests on the CI artifacts GitHub keeps for 90 days. After that an old PR can't be reviewed again.
- The reviewer can be the same model provider as the worker. A different provider is preferred when one is qualified (ENG-154), but the review doesn't wait for it.
- The review job runs in its own cloud session. The factory relies on the reviewer account's lack of write access, not on the session's good behavior, to keep it from changing anything.
