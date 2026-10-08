# The independent review of a worker PR

The loop marks a worker's PR "ready for review" only when someone other than
the worker has said which test assertion shows each acceptance criterion, and
has shown each of those tests failing without the change. In the pilot that
someone is a read-only verifier: a Claude session in this project (or Rolando
himself). It never pushes to the PR, never approves or merges, and never
clears a flag or records an observation; those are Rolando's own, typed at his
terminal by `python3 -m controller.loop run`.

## What the verifier does, per PR

1. Read the approved contract (the one Rolando ran the loop with) and the PR's
   diff on GitHub. Note the PR's head commit, its base commit (the `base.sha`
   GitHub shows for the PR) and the merge base (`compare/<base>...<head>`).
2. For every `automated-check` criterion, find the one test and the one
   assertion in it that shows the criterion's statement holds. Copy the
   assertion exactly as written. Write in a sentence why it shows the
   statement. If no assertion does, map nothing: the criterion stays
   uncovered, and the PR needs a repair, not a generous reading.
3. For each mapped test, run it from the candidate against the merge base's
   product code, in a clean checkout of the pilot repo:

   ```sh
   git checkout <merge base> && git checkout <head> -- tests/ && npm ci --ignore-scripts && npx vitest run <test file> -t "<test name>"
   ```

   Record the outcome: `failed-assertion` (the assertion ran and failed),
   `failed-error` (the test failed before its assertion, for example because
   the function doesn't exist yet) or `passed` (the test doesn't detect the
   missing change; this raises a flag). If running it isn't practical, record
   a `limits` entry saying why instead.
4. Post one comment on the PR holding the block below, with nothing else
   inside the fence. The comment must be posted from an account on the
   mapper list in `verify/review.py` (`rNavarrete`). A session posting through
   Rolando's GitHub connection shows as him, so the comment must also say
   that a Claude session wrote it. A corrected review is a new comment; the
   newest one for the same commit wins.

````text
```factory-review/v1
{
  "contract_digest": "<the approved digest>",
  "commit": "<head>",
  "base_commit": "<base>",
  "merge_base": "<merge base>",
  "links": [
    {"criterion": "ac1", "path": "tests/books.test.ts",
     "test": "<describe name> > <test name>",
     "assertion": "<the assertion, as written>",
     "why": "<why it shows ac1>"}
  ],
  "proofs": [
    {"criterion": "ac1", "path": "tests/books.test.ts",
     "test": "<describe name> > <test name>",
     "outcome": "failed-error",
     "output_excerpt": "<the line that shows it failed>"}
  ],
  "limits": []
}
```
````

`verify.review.review_block()` prints this block from Python.

## What it can't stand in for

- A review for one commit says nothing about the next push. Any new push
  needs a new review.
- Who mapped and who ran the proofs is the comment's author as GitHub
  records it. Names inside the JSON are ignored.
- The block carries mapping work only. Anything in it that looks like a
  clearance, an observation or an approval is not read.
