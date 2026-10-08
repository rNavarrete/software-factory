# ADR 0001: Personal factory operating model and human authority

- Status: **Accepted** (signed off by Rolando 2026-10-07, see sign-off record)
- Date: 2026-10-07
- Linear: [ENG-134](https://linear.app/rolando-projects/issue/ENG-134/define-the-personal-factory-operating-model-and-human-authority)
- Decider: Rolando Navarrete (sole owner and approver)
- Moves to `docs/adr/0001-operating-model.md` in the factory repo once ENG-185 creates it.

## 1. Context

The factory is a personal delivery aid for Rolando, not a platform. v1 runs one repository and one active implementation task at a time through a thin loop: a human-written contract, a small deterministic controller, one Claude Code cloud Routine worker, independent checks, human review and a separately authorized release. Its first job is to show **less active human time per accepted change** than Rolando's current multi-agent workflow (measured in ENG-184, decided in ENG-162). The full Linear backlog (M4 to M8) is optional and is not a commitment.

This ADR fixes who decides what, what evidence each decision needs, and the limits of the first pilot. Every later v1 ticket must stay inside it; changing it needs a new ADR approved by Rolando.

## 2. Decision in one paragraph

Humans own intent, approved scope, exceptions and release; Rolando performs every merge in the pilot. Deterministic controller code owns dispatch validation, durable state and budgets. The worker only proposes changes on a branch. Independent checks, run outside the worker, supply the evidence. Approvals are bound to exact immutable content, and three human decisions stay separate: **approve the contract**, **approve the PR**, **authorize the release**. No approval implies the next one.

## 3. Roles

| Actor | Is | Can | Cannot |
|---|---|---|---|
| **Rolando** | The only human authority | Write and approve contracts, approve PRs, merge, authorize releases, grant exceptions, stop the factory | Delegate contract approval, PR approval, exceptions or release authorization to an agent or rule in v1. (Merge: Rolando merges in the pilot; whether the bot is technically able to merge after approval is recorded by ENG-142, not promised away here) |
| **Controller** | Small deterministic program outside the worker (ENG-136, ENG-185) | Validate approvals, enforce budgets and single-writer ownership, persist state, fire the routine | Approve anything, change a contract, merge, release, or overrule a failed check |
| **Worker** | One Claude Code cloud Routine session under the factory identity (ENG-182, ENG-183) | Read the repo, push a branch, open a PR, report results | Approve, release, edit the ledger, hold production credentials, start new attempts. Must not merge; whatever merge ability the factory identity has is shown and recorded in ENG-142 |
| **Coordinator** | The project's coordinating Claude (build time) and the controller's budget counters (run time) | Track the effort caps in section 8 and the wave board, flag thresholds to Rolando | Decide anything; it only reports |
| **Independent checks** | CI from a pinned trusted source (ENG-140, ENG-142) plus per-criterion review (ENG-156, ENG-157) | Produce pass/fail evidence bound to an exact commit and contract digest | Be modified by the worker. Changes to checks, workflows or test assertions land only through a PR Rolando explicitly reviews and approves as a control change (ENG-143) |

## 4. Responsibility table (one page)

| Area | Decision owner | Who does the work | Required evidence before the step counts as done | Enforced by |
|---|---|---|---|---|
| **Scope** (what to build) | Rolando | Rolando writes the contract (agents may help draft; Rolando's approval makes it real) | Approval record: Rolando's identity, timestamp, canonical contract digest and version, repository, base commit, permitted scope, attempt budget | Controller refuses dispatch without a matching, unexpired, unrevoked approval (ENG-144, ENG-151) |
| **Execution** (making the change) | Controller (mechanically, within the approval) | Worker | Ledger record written *before* firing: task, contract digest, run, attempt number, dispatch intent; then outcome or `launch-outcome-unknown`, session URL, base and candidate commit, PR link | Controller ledger outside the worker's checkout; attempt cap; single-writer lock (ENG-146, ENG-147, ENG-153, ENG-176) |
| **Verification** (is it right) | Rolando (checks and reviewer supply the evidence; they do not decide) | CI and a read-only reviewer (human-assisted is fine in the pilot) | Required CI green from the pinned source on the exact candidate commit; per-criterion report mapping each acceptance criterion to an assertion or human observation, naming contract digest and commit. Worker self-report is never evidence | Ruleset required checks (ENG-142), verifier (ENG-156, ENG-157), bypass tests (ENG-158) |
| **Exceptions** (anything outside the approval: extra attempt, scope change, failed or unknown launch, changed check, protected-file change) | Rolando | Controller stops and escalates; Rolando decides | A written decision in the ledger or on the PR naming the exception and Rolando's choice. A changed contract needs a **fresh approval**, never an edit to the old one. A **repair attempt** (re-running the worker on the same approved contract after a failure) needs Rolando's recorded authorization naming the failure and the attempt number, and counts against the attempt cap; going past the cap is a separate exception that needs a new budget in a fresh approval | Controller blocks on unknown or over-budget states; protected-control changes need Rolando's review (ENG-143) |
| **Merge** (code lands on main) | Rolando | Rolando merges in the pilot. (A bot may technically be able to merge after approval and checks; that is recorded in ENG-142, not relied on.) | Rolando's PR approval on the latest push; stale approvals dismissed; required checks green on that commit; empty bypass list | GitHub ruleset on main (ENG-142) |
| **Release** (reaches production or users) | Rolando | Rolando triggers the release through a human-only mechanism | A release authorization naming the exact tested commit or artifact. Any changed artifact needs fresh authorization. Merge alone never deploys | Release credentials unavailable to worker, controller and any bot; environment gate or equivalent human-only mechanism (ENG-142) |

## 5. Approval binding and separation of decisions

1. **Contract approval is bound to immutable content.** Rolando approves a canonical contract identified by its digest, together with the repository, the exact base commit, the permitted scope and the attempt budget. If any byte of the contract or any of those bound fields changes, the old approval no longer matches and the controller refuses to dispatch until Rolando approves the new version. Approvals can be revoked or expire; the controller checks this at dispatch time (ENG-144, ENG-151).
2. **PR review is a separate decision.** Approving the contract does not approve the resulting code. Rolando reviews the PR on its latest push with the evidence from section 4. A new push after approval invalidates it.
3. **Release authorization is a third, separate decision.** A PR approval, a merge, a green build or a comment does **not** authorize a production deployment. Release needs its own authorization by Rolando, bound to the exact tested commit or artifact, through a mechanism the worker and controller cannot trigger. Until ENG-142 demonstrates such a mechanism, nothing the factory produces is released except by Rolando by hand.
4. **No step infers the next.** "PR exists", "worker says done", "runtime shows success" and "merged" are states, not authorizations.

## 6. No bypass by risk tier

Rolando approves **every** pilot contract and **every** release. There is no low-risk, trivial, documentation-only, dependency-bump or "small change" tier that skips contract approval, PR review or release authorization. Risk tiers, if ever added (optional ENG-159), may only add review, never remove these three gates. Any change to this rule needs a new ADR approved by Rolando.

## 7. First pilot limits

| Setting | Pilot value | Notes |
|---|---|---|
| Task selection | **Manual.** Rolando picks each task and writes or approves its contract | No queue scanning |
| Lanes | **One.** At most one active implementation attempt across the whole factory | Controller enforces single-writer ownership |
| Auto-fix | **Off.** No automatic repair of CI failures or review comments; routine schedules and GitHub event triggers disabled | Each repair attempt needs Rolando's authorization and counts against the cap (default 3 total attempts per task, set in ENG-138). Turning auto-fix on needs the optional bounded-repair experiment and a new decision |
| Intake | **None automatic.** No Linear, GitHub-issue or chat intake listener | Optional ENG-174 is out of v1 |
| Approval interface | **Existing tools only** (an operator command or file plus GitHub's own review UI). No custom approval inbox or Grok widgets | Optional later |
| Repository | One pilot repository chosen in ENG-137 | |
| Parallelism | Building the factory may use parallel agents; the factory itself does not run tasks in parallel | See implementation plan section 5 |

## 8. Maximum setup and maintenance effort (needs Rolando's sign-off)

This is the ceiling on Rolando's own **active** time the factory may cost before it has to prove its value. Active time means hands-on work and focused review: account setup, clicking through settings, writing and approving contracts for build tickets, reviewing build PRs, unblocking agents. Waiting time does not count.

**Approved caps** (Rolando chose these on 2026-10-07 over the drafted 20 hours and 1 hour/week):

- **Setup (through the start of the pilot, ENG-161):** at most **40 active hours** of Rolando's time.
- **Maintenance during the pilot:** at most **2 active hours per week** keeping the factory working (fixing the controller, environment, credentials, CI, routine), separate from time spent on the pilot tasks themselves, which ENG-162 measures.

**What happens at the caps:**

- At 75% of either cap, the coordinator flags it in the project and Rolando decides whether to continue, simplify or cut scope.
- At 100%, new build work pauses until Rolando records a decision: raise the cap (with a reason), simplify, or stop. The cap is never raised silently.
- **Who owns which number:** this ADR owns the ceiling on Rolando's active time (both caps above). ENG-137 records the final pilot-specific effort budget at or below this ceiling once the repo is chosen, and owns the **worthwhile time-saving threshold** used by ENG-162; this ADR deliberately does not set that threshold, because it depends on the pilot repo and the ENG-184 baseline. ENG-138 owns money (subscription, infrastructure, overage) and the attempt default, and uses the 2 hours/week cap as the human-time part of its weekly envelope rather than setting a second number.
- **Counting:** the setup clock starts at Rolando's sign-off of this ADR. Time spent before it (research, planning, Linear rewrite) is noted as sunk cost in ENG-162 but does not count against the 40 hours.
- Tracking: each build thread logs Rolando's active minutes for its ticket (one line per thread, per implementation plan section 5), which also feeds the ENG-184 baseline and ENG-162.

## 9. Platform facts this model depends on (checked 2026-10-07)

These come from current official docs and shape how later tickets must enforce section 4. Where a fact limits a control, the control is recorded as limited, never bypassed.

1. **Routine PRs carry the routine owner's GitHub identity** ([Routines](https://code.claude.com/docs/en/routines)). GitHub does not let a PR author approve their own PR. So Rolando's PR approval only counts if the worker runs under a separate factory account and bot GitHub identity (ENG-183, ENG-180). **If ENG-183 cannot qualify that identity,** cloud rollout pauses until a supported alternative is chosen; required PR approval is not weakened. (Amended 2026-10-07 by Rolando's decision in the ENG-135 thread. The earlier fallback of zero required approvals plus Rolando's own merge was dropped because a worker identity with write access could then merge its own PR once checks pass. See planning/governance-map.md §4.1.) This ADR's three-decision rule still holds either way.
2. **The fire API has no idempotency key and there is no documented way to cancel a running session** ([fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire)). Attempt caps therefore block *new* launches only; an ambiguous launch becomes `launch-outcome-unknown` and stops for Rolando (ENG-146, ENG-153). Routines are a research preview, so ENG-136 keeps an exit path.
3. **Routines run without permission prompts**; what a run can reach is set only by the selected repos, environment network level and included connectors (all connectors are included by default). ENG-181 must strip connectors and narrow network access; the worker's limits come from those boundaries and GitHub rules, not from prompts.
4. **The GitHub proxy blocks tag pushes and branch deletion but not pushes to other branches**, and a ruleset the worker's identity can bypass does not stop it. Hence the empty bypass list in ENG-142.
5. **GitHub deployment-environment required reviewers are not available for private repos on Free/Pro/Team** ([environments](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments)). If the pilot repo is private on those plans, the human-only release mechanism in ENG-142 must be something else: for example a tag or manual `workflow_dispatch` deploy limited by deployment tag rules, with tag creation restricted by a tag ruleset to Rolando, or Rolando deploying by hand with credentials the factory never holds.
6. **Auto-fix is a per-PR toggle with no documented repo-wide off switch**; installing the Claude GitHub App enables it as an option ([auto-fix](https://code.claude.com/docs/en/claude-code-on-the-web#auto-fix-pull-requests)). "Auto-fix off" is enforced by policy: routine prompts never ask Claude to watch PRs, no one runs `/autofix-pr`, and ENG-182 disables GitHub event triggers. ENG-158 should check that no factory PR has auto-fix on.

## 10. Consequences

- Rolando is the bottleneck by design: every task passes through him at least four times (contract approval, PR review, merge, release authorization), plus once per authorized repair. That is acceptable because the pilot is small and the goal is to measure his time honestly, not to remove him.
- Several controls depend on GitHub and Routine behaviour that later tickets must demonstrate (bot merge authority, release gate, launch idempotency). Where a control cannot be demonstrated, it is recorded as a limitation, never bypassed or assumed.
- Anything outside this ADR (parallel lanes, auto-fix, automatic intake, risk-based skipping) is out of v1 and needs ENG-162's stop/simplify/continue decision plus a new ADR.

## 11. Acceptance criteria trace (ENG-134)

| Criterion | Where answered |
|---|---|
| One-page responsibility table names owner and required evidence for scope, execution, verification, exceptions, merge and release | Section 4 |
| Plan approval bound to an immutable contract; PR review and release authorization distinct; PR approval alone does not authorize production deployment | Section 5 (items 1 to 4), Section 4 rows Merge and Release |
| Rolando approves every pilot contract and release; no risk tier bypasses the gates | Section 6 |
| First pilot: manual selection, one lane, auto-fix off, no automatic intake or custom approval inbox | Section 7 |
| Rolando signs off on the operating model and maximum setup/maintenance effort before implementation | Section 8 and the sign-off record below |

## Sign-off record

Sign-off applies to this file as it stands at the version below; any later edit to sections 2 to 8 needs a fresh sign-off.

- Version signed: `883d28aae63a7cd1c41696542ff44efd749f36e675f199d3771a4abb9dcc79c9` (sha256 of this file above the sign-off record)
- Operating model (sections 2 to 7): approved
- Maximum setup/maintenance effort (section 8): approved with other caps: 40 active hours setup, 2 active hours/week maintenance
- Signed by / date: Rolando Navarrete, 2026-10-07 (decision card in the ENG-134 project thread)
