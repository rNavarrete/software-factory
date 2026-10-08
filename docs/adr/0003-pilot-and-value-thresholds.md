# ADR 0003: Pilot repository, tasks and value thresholds

- Status: **Accepted** (signed off by Rolando 2026-10-07)
- Date: 2026-10-07
- Linear: [ENG-137](https://linear.app/rolando-projects/issue/ENG-137/choose-a-small-pilot-and-set-its-value-thresholds)
- Decider: Rolando Navarrete
- Bounded by: [ADR 0001](0001-operating-model.md). This ADR may lower ADR 0001's effort caps, never raise them.
- Moves to `docs/adr/0003-pilot-and-value-thresholds.md` in the factory repo once ENG-185 creates it.

## 1. Decision

The pilot runs on a **new, purpose-built public demo repository**, not on a product repo. It has its own Linear project holding 8 tasks in 4 matched pairs. Rolando chose this in the ENG-137 thread on 2026-10-07 ("can we just create a brand new demo repo with a demo linear board"). He set the value threshold at 30% on a decision card the same day.

## 2. Repository, stack, access model, release destination

| Item | Choice |
|---|---|
| Repository | `rNavarrete/factory-pilot-demo` (**public**, created by Rolando 2026-10-07; scaffold pushed at `f841647`, first CI run green). Contents: a small "Reading List" web app |
| Stack | TypeScript, Vite, Vitest (jsdom), no framework. Data in browser localStorage, no backend, no secrets |
| Existing checks | `.github/workflows/ci.yml`: `npm ci`, typecheck, tests, build on every PR and on main. ENG-140 hardens this into the trusted independent check |
| Access model | Rolando owns the repo. The worker pushes branches and opens PRs only under the ENG-183 factory identity via ENG-180's GitHub access. The Claude GitHub App must be installed on this repo. Rolando approves PRs and merges (ADR 0001 §4). Auto-fix stays off on every PR (ADR 0001 §9.6). Because the repo is public, anyone can open fork PRs; their CI runs read-only with no secrets, which is harmless, and the factory ignores PRs it did not open |
| Release destination | GitHub Pages, through `.github/workflows/release.yml`. It runs only on manual `workflow_dispatch`, and its `deploy` job uses the `github-pages` environment. Merging never deploys |
| Release gate | Because the repo is **public**, GitHub environment required reviewers are available on every plan (ADR 0001 §9.5). ENG-142 adds Rolando as the only required reviewer on `github-pages`, with self-review allowed for him and no bypass. The environment's deployment branches are limited to `main`, and before approving, Rolando checks that the run's commit SHA is the one that passed CI. That binds the release to the exact tested commit (ADR 0001 §5.3). Pages source must be set to "GitHub Actions" |

**Why a demo repo:** it is small and reversible, and nothing real can break. Tests, CI and a release path exist before the factory starts. Both arms of the comparison work on the same unfamiliar codebase. Going public resolves the release-gate limitation in ADR 0001 §9.5 at no cost.

**Candidates rejected:** the Chef Next Door app (private and not visible to Claude), labubuAPI (no tests or CI), Ice Radar `web` (a mid-size app, which ENG-137 rules out), sora-extender (too thin), and the chef-next-door static site (too trivial).

**Known costs of a demo repo:** the tasks are invented rather than drawn from Rolando's real backlog. The savings measured are therefore evidence about the loop's mechanics, not proof that it pays off on product work. ENG-162 must say so, and if the result is "continue", the next step is a second pilot on a real repo (a decision for ENG-162, not v1 scope). The repo is public, so it must never hold secrets. The release workflow holds none, because Pages deploys use OIDC.

## 3. Pilot tasks

Linear project: [Factory Pilot Demo](https://linear.app/rolando-projects/project/factory-pilot-demo-741b375d401e). This is a list Rolando picks from by hand, not an intake queue (ADR 0001 §7).

| Pair | Kind | Baseline arm (current workflow, ENG-184) | Factory arm (ENG-161) | Difficulty difference |
|---|---|---|---|---|
| A | UI + small logic | [ENG-186](https://linear.app/rolando-projects/issue/ENG-186) Show how many books are still to read (1 pt) | [ENG-191](https://linear.app/rolando-projects/issue/ENG-191) Show how many books were finished this year (1 pt) | Factory task slightly harder (date/year logic) |
| B | Logic + control | [ENG-192](https://linear.app/rolando-projects/issue/ENG-192) Filter the list by status (2 pts) | [ENG-187](https://linear.app/rolando-projects/issue/ENG-187) Sort by title, author or date added (2 pts) | Baseline task slightly harder (status change while filtered) |
| C | Data export | [ENG-193](https://linear.app/rolando-projects/issue/ENG-193) Export the list as CSV (2 pts) | [ENG-188](https://linear.app/rolando-projects/issue/ENG-188) Export the list as Markdown (2 pts) | About equal; CSV escaping vs Markdown escaping |
| D | Bug fix (seeded) | [ENG-190](https://linear.app/rolando-projects/issue/ENG-190) Added dates show the wrong month (1 pt) | [ENG-189](https://linear.app/rolando-projects/issue/ENG-189) Duplicate and padded titles are accepted (1 pt) | Factory task harder (three cases vs a mostly one-line cause; ENG-190 also has a time zone subtlety) |

Accepted user outcomes, in short (full text and test evidence in each Linear issue):

- ENG-186: header shows "N to read" and updates live.
- ENG-191: header shows "N finished in YYYY" for the current year only.
- ENG-192: filter All/To read/Reading/Done that survives reload and behaves correctly when a status changes.
- ENG-187: sort by date added/title/author, case-insensitive, survives reload.
- ENG-193: "Export CSV" downloads a correctly escaped reading-list.csv.
- ENG-188: "Export Markdown" downloads reading-list.md grouped by status.
- ENG-190: list dates show the correct month in local time.
- ENG-189: titles and authors are trimmed; case-insensitive duplicates are rejected with a message.

Each issue states its **accepted user outcome**: what Rolando can see on the released site, plus the test evidence. A task counts as accepted only when that outcome is visible on the released Pages site and Rolando has approved, merged and authorized the release.

The two bugs are deliberately seeded in the scaffold (`formatDate` uses a zero-based month; `addBook` neither trims nor de-duplicates). The existing tests do not catch them.

Across the pairs the difficulty differences roughly cancel out (factory arm harder in A and D, baseline harder in B). ENG-162 should still compare pair by pair, not only totals.

## 4. Learning effects and run order

- The baseline arm could run now, but the factory arm can only run after ENG-176/145. If every baseline task ran first, Rolando would know the codebase better by the time the factory ran, which biases the result toward the factory.
- **Rule:** run baseline tasks **during the ENG-161 pilot window, interleaved** with factory tasks, alternating which arm goes first in each pair: A baseline first, B factory first, C baseline first, D factory first.
- Pair C shares `formatDate` with the pair D bug. Whichever task runs later inherits the fix; note it in the log.
- Record per task: arm, start and end of Rolando's active minutes, interventions, repair attempts, and whether it was accepted first time.

## 5. Effort budget (pilot-specific, within ADR 0001)

- **Setup:** 40 active hours (ADR 0001 ceiling, kept, not lowered). Clock started at ADR 0001 sign-off.
- **Maintenance during the pilot:** 2 active hours/week (ADR 0001 ceiling, kept).
- The 75% flag and 100% pause rules from ADR 0001 §8 apply unchanged.

## 6. Worthwhile time saving and go/no-go (input to ENG-162)

**Metric:** for each pair, saving = 1 - (factory-arm minutes / baseline-arm minutes), counting Rolando's active minutes to an accepted change. The headline number is the **median of the per-pair savings**, which handles the 1-point vs 2-point mix better than pooled minutes. The pooled median minutes per accepted change is reported alongside.

| Result | Saving vs baseline | Decision |
|---|---|---|
| Continue | **30% or more** | Keep the loop and pick one optional M4-M8 ticket that fixes a measured bottleneck |
| Simplify | 10% to under 30% | Cut controller or ceremony that cost the most minutes, then re-measure |
| Stop | under 10% | Stop building the factory and keep the current workflow |

**Overrides** (apply whatever the saving):
- Any contract approval, PR approval or release authorization that was bypassed, or anything released without Rolando's authorization → **stop or simplify**, never continue.
- Setup over the 40-hour cap without a recorded decision → stop.
- Fewer than 3 pairs completed → no decision; extend the pilot or stop.

**Evidence standard:** with 4 pairs this is directional, not statistical. ENG-162 reports the per-pair numbers next to the median, and says what the demo-repo caveat (§2) means for the decision.

## 7. Acceptance criteria trace (ENG-137)

| Criterion | Where |
|---|---|
| Repository, stack, access model and release destination explicitly selected | §2 |
| About 5-10 comparable small tasks, with differences in difficulty and learning effects documented | §3, §4 (8 tasks, 4 pairs) |
| Maximum setup/maintenance effort and worthwhile time-saving threshold set before building; sample treated as directional | §5, §6 |
| Accepted user outcome per task and go/no-go criteria for ENG-162 | §3 (and each Linear issue), §6 |

## 8. What other tickets inherit

- **ENG-139/140/141/142/143** work in `rNavarrete/factory-pilot-demo`. ENG-142 configures the `github-pages` environment reviewer, the main ruleset, and the `workflow_dispatch` permissions.
- **ENG-180** installs the Claude GitHub App and bot access on this repo.
- **ENG-184** records baseline minutes on the four baseline-arm tasks, following the run order in §4.

## Sign-off record

- Choices: demo repo + demo Linear board (Rolando, thread message 2026-10-07 21:18Z); 30% saving threshold (decision card 2026-10-07 21:15Z).
- Write-up signed off by Rolando: "ok" in the ENG-137 thread, 2026-10-07 22:51Z, before any build work in the pilot repo.
- Critic pass (separate reviewer agent, 2026-10-07): all four criteria answered after fixes; no conflict with ADR 0001.
- Rolando's active time on ENG-137: about 5 minutes (two decisions, creating the repo, one sign-off).
