# The automatic independent review

When a worker opens a PR, the factory starts an independent review of it by itself. The review is done by OpenAI's Codex, running in GitHub's cloud as a protected workflow in this repository. This page says what the review does, what it can and can't do, and what has to be set up before it runs for real.

## What it does

Each round, the factory reads the PR again from GitHub (`controller/review/reviewer.py`):

1. **Can it be judged at all?** The PR must be open, aimed at the pilot's `main`, from the attempt's own branch, opened by the worker, with the approved contract's markers. Anything else stops here and is reported straight away, before any review is started. A failed CI run or a wrong title or body marker is something a correction can fix, so it goes to the repair step. Anything else (another repository or branch, a closed PR, an author that isn't the worker, CI evidence that can't be read) goes to Rolando.
2. **Which revision is it?** The contract digest, the PR's head commit, its base commit and the merge base, plus the PR number, make a request key. The same key always means the same review. It is started at most once, and a verdict already recorded for it is reused. A new push or a moved base is a new key.
3. **Is CI finished?** While CI runs, the factory waits. When CI or a gate (scope, base, checks) has already failed, the verdict is "failed" at once, with findings for the repair step. No review is spent on it.
4. **Start one review.** The factory writes its claim to the ledger, then starts one run of `.github/workflows/codex-review.yml` with the request (`controller/review/workflow.py`). The claim comes first, so a crash, a restart or a second copy of the service never starts it twice. If the answer to the start is lost, the factory never starts it again: it looks for the run by its key instead, and reads its result if one appears.
5. **Read the result.** The factory reads the run's one result file and checks where it came from (below). The two existing verifiers (`verify/criteria.py`, `verify/assertions.py`) then decide, using Codex's mapping of each criterion to a test assertion, the measured failure proofs, and Codex's findings. PR comments are not read at all, so no comment from anyone counts as a review.

The verdict is one of these:

- **passed**
- **failed**: findings for a correction.
- **needs Rolando**: a behavior only he can check, a change to a protected control, a product or security question, or context the review couldn't see.
- **unknown**: the start was lost, the run failed, or its result was missing, malformed, for something else, or incomplete.
- **blocked**: an allowance is used up, the factory is on hold, or the usage reading is high or old.
- **not reviewable**

Each verdict names the exact commit it covers. A passed verdict is evidence for Rolando, never an approval. The reviewer can't approve, merge or release, and nothing here records a decision.

## The review workflow

The workflow has three jobs:

- **review** checks the request, checks out the PR at the exact head commit, removes any `AGENTS*.md` files and `.codex/` folder from that checkout (Codex would otherwise take them as its own instructions), and runs the official Codex GitHub Action (pinned to a commit) with the fixed instructions in `controller/review/codex_prompt.md` and the output schema in `controller/review/codex_schema.json`. Codex runs read-only, with sudo dropped, as the action's security guide advises. The OpenAI key is held by the action's proxy, not by Codex. No pilot script, install step or test runs in this job. Its token can only read this repository.
- **proofs** runs the tests Codex linked (test files under `tests/` only), taken from the PR's head, against the base's product code. It shows whether each test would have caught the missing change. This job runs the worker's code, so it gets no secrets and no token permissions, Docker is switched off, and that code runs as a separate user that can't use sudo or write the job's outputs. If the install fails, a test file is missing, a test doesn't run or its result can't be read, the whole review result is **unknown**: a measuring problem is never counted as a failing test.
- **publish** puts both outputs, with the run's own identity, into one `factory-review-result` artifact. It runs no PR code and gets no key.

What Codex is given: the approved contract (goal, criteria, allowed paths and actions), the exact revision, the trusted CI run's results, and on a verification pass the open findings. It reads the code and diff itself. It must report on behavior, test coverage of each criterion, regressions and edge cases, scope, weakened tests or changed controls, security and secrets, and claims the code doesn't back up. A review that doesn't cover every area and every criterion is incomplete.

## Why a result can be trusted

The worker can't change any of this. It has no access to the factory repository, so it can't edit the workflow, the instructions, the schema or the checking code, and it can't start a run. The factory's `main` needs a reviewed PR.

The factory doesn't believe what a run says about itself. It takes the run's identity from GitHub's own record of the run, and every check below must hold:

- The run is in the factory repository, ran `.github/workflows/codex-review.yml`, and was started by a dispatch.
- It ran from `main`, and its workflow commit is on the factory's `main`.
- It was started and run by the trusted dispatcher (the owner of the review token).
- It was created after the factory claimed the review, and its title carries the request key.

Then it reads the run's artifact. There must be exactly one `factory-review-result`, unexpired, holding only `result.json`. Its copies of the run id, run attempt, workflow commit and workflow file must match GitHub's record. The request it answers must be the exact text the factory sent (the hash recorded in the ledger), for the same key, PR, contract and commits, and it must have used the model and effort the factory is set to. If several trusted runs answer the same request, the newest one that finished cleanly counts.

Anything that fails a check above is ignored, so a look-alike run or comment changes nothing. A run that passes them but gave no usable result is **unknown**, never a pass. That covers a failed or cancelled run, a missing or doubled artifact, unreadable JSON, a result for another request, or Codex output that is incomplete. "No findings", valid JSON or a finished job is never enough. A pass still needs every criterion mapped to an assertion and shown to fail without the change, or a stated reason why that isn't practical. A criterion that has a linked test needs that test's result; a stated reason doesn't replace it.

## Findings

Every finding has these fields (`verify/findings.py`):

- **id**: stays the same when the same problem appears on a new commit.
- **severity**: blocking or advisory.
- **category**
- **route**: repair or Rolando.
- **summary** and **evidence**: Codex's file and line are put at the start of the evidence.
- **suggested action**
- **commit**: the exact commit it was found on.
- **resolved**

Code, test and scope findings go to a correction. Product and security findings, and context Codex couldn't see, go to Rolando. A criterion Codex judges unmet always leaves a blocking finding.

When a correction pushes a new revision, the next review is a verification pass. It gets the open findings and checks them on the new commit. A finding is marked resolved, once, only when a review of a later revision no longer raises it. The worker saying "fixed" resolves nothing. A push whose CI fails, CI that is still running, or a run that gives no usable result leaves every open finding open.

On a verification pass, Codex may repeat an earlier finding by its id, so rewording it doesn't count as fixing it. That id is only accepted when it names an open finding of the same category from the same reviewer.

Every result is recorded, including "waiting for CI" and "not reviewable". So once a PR is pushed again, retargeted, or closed without merging, an earlier pass is no longer the latest evidence. A merged PR keeps the verdict for its last revision only if the merged revision is exactly the one reviewed. If the result a pass rested on disappears, the pass is withdrawn.

## Allowances and cost

The defaults are one full review and one verification pass per task and approved contract. More passes need an explicit allowance (`ReviewPolicy.extra_passes`). The factory never loops until reviewers agree.

The unit is one review run: one start of the workflow, which makes one Codex run. Each counts in the same weekly allowance as worker launches (12 a week), and the same holds, waits and usage reading stop it. A start that was refused still counts. A start that GitHub turned away for its rate limit records a wait and doesn't use up the review's own tries.

A start that definitely didn't happen (for example, a missing token) may be tried once more. A lost one is never started again.

Codex runs on OpenAI API billing, paid per use from the OpenAI account whose key is set up below. It is not covered by a ChatGPT subscription. What one review costs depends on the model and the size of the PR; the first qualification run measures it. To cap spending, use a separate OpenAI project for the factory with a monthly budget.

## What must be set up before it runs for real

Until the review is set up, nothing is started and the factory uses its stand-in reviewer.

1. **An OpenAI API key** from a separate OpenAI project for the factory, with billing and a monthly budget set by Rolando.
2. **A review token**: a fine-grained GitHub token on Rolando's account, limited to `software-factory`, with **Actions: read and write** and **Contents: read**. It can start and read the review workflow and nothing else; it can't change code in either repository.
3. **The pilot read token** (`github-token`) also needs **Actions: read** on the pilot, so the review can read CI's evidence files.
4. **One command**, `sh deploy/fly/setup-reviewer.sh`, run on `main` after this change is merged. It does these steps:
   - creates the `codex-review` environment, which only `main` can use;
   - stores the OpenAI key there, typed at a hidden prompt;
   - stores the review token, the dispatcher (your GitHub login) and the model in Fly's secrets, with the token typed at a hidden prompt;
   - redeploys;
   - asks GitHub whether the review token may start the review workflow, without starting it, and stops if it can't (`python3 -m controller.review check-token` on the host). `sh deploy/fly/go-live.sh` runs the same check before it changes anything. A token with Actions set to read-only is refused here instead of at the first worker PR.
5. **Rolando's answers.** The review needs his recorded observations of behavior only a person can check, and his clearances of protected-control changes. Until it is given a source for them (the Linear observation replies from the progress-reporting work), a task with either stays at "needs Rolando".

The model is a setting (`FACTORY_REVIEW_MODEL`, default `gpt-6.1-sol`, which OpenAI's Codex model list showed with API access on 2026-10-08). Check the list again before turning the review on. The Codex CLI version is pinned in the workflow.

## What Rolando sees on the ticket

Each round the service asks the review where it stands (`check`). It posts each new state on each revision once, using the progress-reporting messages (`controller/review/report.py`):

- **Reviewing:** the Codex review has started, as a full review or as a verification pass on a corrected version.
- **Ready for your review:** it passed, naming the exact commit and linking the run. For a corrected version it says how many earlier findings were checked as fixed.
- **Failed:** how many problems a correction has to fix, and what they are. It never says a correction is running; that is said only when one actually starts. Nothing is asked of Rolando.
- **Needs your decision:** one observation request per behavior only he can check, bound to that commit, plus one note listing anything else only he can settle.
- **Waiting, Unclear or Notice:** the review is blocked (for example, its passes are used up), its result is unusable, or the PR can't be reviewed as it is.

When the PR is merged, the merge record compares the review's commit with the PR's last commit as GitHub shows it. It counts as verified work only when the review passed on exactly that commit. Anything else is recorded as an exception that says what is missing.

## Trying it without starting anything

```
python3 -m controller.review dry-run tasks/samples/clear-finished.json 15 --as-open
```

This reads the PR with `gh api` as you. It uses a throwaway ledger, sends nothing, and prints the request key, the verdict, any findings and how many reviews would have been claimed.

## Limits

- The review rests on the CI artifacts GitHub keeps for 90 days, and on the review's own result, also kept for 90 days. After that an old PR can't be reviewed again.
- Each move of main is a new revision, so it uses a review pass. After one full and one verification pass, a further move of main needs an explicit allowance.
- The failure proofs run the worker's own code, and the results are what that code reports about itself. A test written to fail only on the base would look like a good proof. The proofs show the tests aren't trivially written to pass; they don't prove the worker's code is honest. Codex's review of the tests is the check on that.
- Agent instruction files are removed before Codex starts, but everything else in the PR (code comments, README, test names) is still read by the model. Its instructions say to treat all of it as data, which is a request to the model, not something enforced. That is why a pass is evidence for Rolando, not an approval.
- Changing the model or effort setting while a review is running makes that review's result **unknown**.
- Codex is a different provider from the worker, but that alone doesn't make it independent. Independence comes from the separate repository, roles, credentials and the checks above.
- GitHub records the review runs as started by the owner of the review token (Rolando's account). A run Rolando starts by hand with the same key counts the same way.
- A GitHub rate limit on starting a review also makes worker launches wait, because the factory has one wait.
