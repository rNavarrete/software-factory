# How the factory drafts a task from a Linear ticket

Rolando writes what he wants and how to tell it's done, then moves the ticket to Todo. The factory works out the technical side itself: which files a worker may change, which commit it starts from, which checks prove each criterion, and how many attempts it gets. It only comes back to him with product questions. This is ENG-175, in `controller/prepare/`.

## What Rolando writes

A title, an outcome, and a checklist of acceptance criteria:

```markdown
## Outcome
Let the reader correct a book's title or author.

## Acceptance criteria
- [ ] renameBook(books, id, title, author) returns a new array with that book changed.
- [ ] renameBook throws an Error when the title is empty.
- [ ] The page has an Edit button on each book that saves the change.
```

The heading can also be "Done when" or "Definition of done", in bold instead of `##`, and items can be numbered. If there is no such heading, checklist lines (`- [ ]`) anywhere in the description count. Other sections ("Notes", "Context") go to the worker as context, marked as not being instructions.

He never names files, writes JSON, picks commits or maps criteria to tests.

## What happens after the Todo move

1. **Read the exact text.** The factory reads the ticket and checks that it still has the same revision ENG-174 recorded when Rolando moved it (title, description, labels, project, team and parent). If anything changed, it drafts nothing and says so on the ticket. Moving it to Todo again authorizes the new text.
2. **Screen it.** It stops and asks Rolando only when:
   - there are no acceptance criteria, or a criterion is still an open question ("?", TBD, "not sure"), repeated, or far too long;
   - the ticket is too big for one task (more than 6 criteria, or more than 8,000 characters). It then proposes a split that keeps his criteria in order, and nothing starts until he decides;
   - the ticket asks for something the project's policy doesn't allow: a protected file (CI workflows, `package.json`, `CLAUDE.md`, compiler and build config…), a folder no factory task may write (such as `public/`), a new dependency, or something the factory never does (merge, release, deploy to production, push to main, skip tests, ignore instructions). It asks whether to drop that part or wait.
   The screen reads the cleaned text, so invisible characters and mention tags can't hide a phrase from it.
   Silence is never an answer. A question closes the work, and nothing starts until he moves the ticket to Todo again.
3. **Choose the base.** It asks GitHub for the newest commit on `main` whose own run of `.github/workflows/ci.yml` passed the `verified` job, looking back at most 20 commits. Every commit on main came through Rolando's reviewed PR. It asks fresh every time, so no old sample base is reused. If none is green, it waits and tries again next round.
4. **Draft.** Paths come from the policy's writable list (`src/**`, `tests/**`, `index.html`, `README.md`, `docs/**` for the pilot), never from the ticket. Actions are what the project allows, limited to changing, adding and testing files. Dependencies and control files need Rolando's explicit word, and deleting files isn't offered in v1. The attempt budget is the project's. Each criterion keeps Rolando's exact words. A criterion about a function is proved by `npm test`. One about the page is checked by using the built app. One about documentation is checked by reading it.
5. **Review.** Separate checks, which don't trust the drafter, confirm that the contract validates; that repository, base, task, budget, checks and actions are exactly what the policy and onboarding give; that every path is writable and none is protected; and that the criteria are all of his lines, in order and unchanged. If any check fails, nothing is returned, and the ticket says the factory couldn't prepare it. A missing drafting policy, a policy whose branch isn't the onboarded one, GitHub being unreachable or no green commit on main are retried every round instead, because they can fix themselves.
6. **Summarize.** The service saves the contract, named by its digest, and posts a short summary on the ticket: what may change, the starting commit, how each criterion will be checked, and the attempt budget. The summary doesn't ask for approval.

## Trace

The contract's `notes` name the Linear ticket, its exact revision, the Todo move's event id, the drafting policy's sha256 and the CI run that made the base eligible. They are part of the contract, so its digest binds them. The service's ledger then links that digest to the attempt, and the attempt to its PR (`[<task> a<n> <digest12>]` in the PR title).

Preparing a task approves, merges, releases and grants nothing. The service still checks the contract against the onboarding entry, and fires it only through the dispatcher's approval check and attempt gate.

## The drafting policy

`deploy/pilot/drafting.json` (format `factory-drafting/v1`), one entry per onboarded Linear project:

| Field | Meaning |
|---|---|
| `writable_paths` | The only paths a contract may grant. They may not overlap `protected_paths` |
| `protected_paths` | Never granted. The screen also asks about tickets that name them |
| `verification_commands`, `test_command` | The checks every contract runs, and the one that proves logic criteria |
| `escalate_to` | GitHub login written into the contract |
| `max_criteria`, `max_ticket_chars` | Above these, the factory proposes a split |
| `base` | Branch, trusted workflow file, required job, and how far back to look |
| `worker` | What a worker needs to run this repository. ENG-154 reads it, and it is recorded in the notes |

Rolando changes it by hand, the same way as the onboarding file. Ticket text can't change it.

## Where it plugs in

`Preparer(reader, github, policy)` implements the service's `ContractPreparer` seam (`controller/service/seams.py`). `reader` is anything with `fetch(issue_id)`. `LinearTicketReader` does that over a GraphQL `query(text, variables)` callable, such as ENG-174's Linear client. `github` is the read-only `GitHubApi` the loop already uses. A model-based drafter can replace `RuleDrafter` through the `Drafter` protocol later, and its output would pass the same review.

This code reads ticket text, so it runs in the service process, which doesn't hold the approval key (ENG-174's signer split).

## Known limits

- **Answers in comments aren't read yet.** The factory drafts only from the ticket text. So each question asks Rolando to put his answer into the ticket and move it to Todo again. ENG-178's `DecisionReader` is there for when answers should feed drafting.
- **The scope screen is cautious.** A ticket that only mentions a protected file ("README explains the scripts in package.json") still gets a scope question. That only ever stops work, and it never widens a contract.
- **Not every dependency request is spotted.** "Install lodash and use it" doesn't use the word "dependency", so the screen misses it. The worker still can't add one: `package.json` is protected, the contract has no `add-dependency` action, and the verifier flags any change to it.
- **Tickets that are much too long aren't parsed.** Past twice the length limit, the factory asks for a shorter ticket without proposing a split.
