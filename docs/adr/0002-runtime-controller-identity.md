# ADR 0002: Cloud runtime and minimal controller

- Status (two separate states):
  - **Architecture:** **Accepted** (signed off by Rolando 2026-10-08 01:31Z, after two review rounds; see sign-off record).
  - **Runtime qualification:** **Conditional.** The live cloud-path run passed except the auto-fix check (section 10). Cloud rollout and pilot dispatch stay blocked until ENG-182's first factory routine PR shows auto-fix off. Accepting the architecture does not lift this block.
- Date: 2026-10-07
- Linear: [ENG-136](https://linear.app/rolando-projects/issue/ENG-136/qualify-the-cloud-runtime-and-minimal-controller)
- Decider: Rolando Navarrete
- Builds on: [ADR 0001](0001-operating-model.md) (accepted). Nothing here changes ADR 0001 sections 2 to 8.
- Moves to `docs/adr/0002-runtime-controller-identity.md` in the factory repo once ENG-185 creates it. Section 11 holds ENG-183's identity GO (ENG-183 is Done).

## 1. Context

ADR 0001 fixed who decides what. This ADR fixes **what runs the worker, what runs the controller, where state and secrets live, and what the runtime can and cannot be trusted to do.** The chosen direction from ENG-136 is Claude Code cloud Routines for one bounded task per run, fired by a small deterministic controller that lives outside the worker.

Routines are a research preview: "Behavior, limits, and the API surface may change" ([Routines](https://code.claude.com/docs/en/routines)). The fire endpoint is "an experimental API" ([fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire)). So this ADR also fixes the exit path and the events that force requalification.

## 2. Decision in one paragraph

The worker is **one Claude Code cloud Routine** with an API trigger only, one repository, a dedicated locked-down cloud environment and no connectors, owned by the factory Claude account with the bot GitHub identity. If ENG-183 cannot qualify that identity, cloud rollout pauses until a supported alternative is chosen (ADR 0001 section 9 item 1, as amended); required PR approval is never weakened. The controller is a **single-process Python 3 CLI using only the standard library**, run by Rolando on his own workstation when he chooses to (no daemon, no server, no webhook listener). State lives in a **SQLite ledger outside every git checkout**; the routine token lives in the **OS keychain**. The controller makes **no model calls**; one worker session performs the implementation. Because the platform gives a fire-only API with no idempotency, no read access and no documented cancel, the controller **never retries an ambiguous launch, never claims it can stop a running worker, and treats GitHub as the only machine-readable outcome evidence**.

## 3. Exact runtime, account, host, storage and secrets

| Concern | Decision | Why / notes |
|---|---|---|
| **Worker runtime** | Claude Code cloud Routine, `anthropic_cloud` VM (4 vCPU / 16 GB / 30 GB per docs), Claude Code CLI version recorded per run from the session (observed 2.1.293 on 2026-10-07) | Chosen direction. Run-time CLI version is not pinnable by us; it is recorded and is a requalification trigger (section 9) |
| **Routine shape** | One routine named `factory-worker-<pilot-repo>`. Triggers: **API only**. No schedule, no GitHub event triggers. Repositories: the pilot repo only. Connectors: **none** (removed explicitly; they are included by default). Saved prompt opts in to acting on the `<routine-fire-payload>` block and tells the worker to push only to `claude/<task-id>-a<attempt>` and to put `[<task-id> a<attempt> <digest12>]` in the PR title | Single-lane, auto-fix off (ADR 0001 section 7). The branch/title marker is the reconciliation key, since nothing else is readable |
| **Cloud environment** | A new dedicated environment `factory` (ENG-181), not `Default`. Network: **Custom** allowlist (only the package registry the pilot stack needs; GitHub goes through the GitHub proxy and needs no entry), or **None** if the pilot needs no installs. No environment variables holding secrets. Setup script installs only what the pilot's checks need | Observed 2026-10-07: the account has one environment, `Default`, "trusted network access". Environment variables are readable by anyone using the environment, so none hold secrets |
| **Account / identity** | Target: the **factory Claude account** with the bot GitHub identity (ENG-183, ENG-180). Qualification (Appendix A) runs on the **factory account** too, so the probe exercises the real identity path | Docs: routines "belong to your individual claude.ai account … commits and pull requests carry your GitHub user". ENG-183 records go/no-go in section 11. No-go means cloud rollout pauses (ADR 0001 section 9 item 1, amended 2026-10-07); there is no run-as-Rolando fallback for real tasks |
| **Controller host** | Rolando's own workstation, run by hand (`factory dispatch <contract>`, `factory reconcile <run>`, `factory status`). macOS on an Apple-silicon MacBook Pro (confirmed 2026-10-07 from the linked device); the Keychain calls are macOS-only | ADR 0001 says operator-invoked. API-only trigger means nothing needs to listen, so no always-on host, no inbound port, no cloud bill |
| **Controller language** | Python 3.11+ standard library only (`sqlite3`, `hashlib`, `json`, `urllib.request`, `subprocess` for `security`/`gh`). No third-party packages in the critical path | Small, auditable, deterministic, no supply chain in the trust path. ENG-185 builds the skeleton |
| **Durable storage** | SQLite file at `~/.software-factory/ledger.db` (mode 0600, WAL, `synchronous=FULL`), **outside every git checkout**. Append-only `events` table plus derived state tables; approved contracts stored content-addressed by sha256 in `~/.software-factory/contracts/`. Backup: SQLite online backup after each dispatch and reconcile to `~/.software-factory/backups/` (covered by Rolando's normal machine backup) | Worker never sees it (it runs in a different machine). Single writer is the operator process; a file lock guards concurrent CLI invocations (ENG-146/147) |
| **Secret storage** | Routine fire token (`sk-ant-oat01-…`, one routine only, write-only) in the **macOS Keychain**, generic password with service `software-factory` and account `routine-token/<trig_id>`, read at fire time via `security find-generic-password -w`. Never in env files, repo, shell history, ledger or logs. The ledger stores only `sha256(token)[:12]` as a fingerprint so a rotated token is visible | Docs: token "shown once and cannot be retrieved later"; "Generating a new token revokes the previous one". Release credentials never exist on this host (ADR 0001) |
| **Outcome evidence source** | GitHub REST (`gh api repos/<repo>/…`) under Rolando's read token: branches matching `claude/<task-id>-a<attempt>`, PRs whose title carries the marker, head commit SHA, check runs | Fire token has "no read access" and no public session API exists. GitHub is the only machine-readable evidence |

### Controller transitions make no model calls

| Transition | Done by | Inputs |
|---|---|---|
| contract approved → dispatch allowed | controller code | approval record, contract digest, base commit, attempt budget (ENG-144, ENG-151) |
| dispatch intent recorded → fire | controller code | ledger write committed **before** the HTTP call (ADR 0001 section 4) |
| fire response → `launched` / `not-launched` / `launch-outcome-unknown` | controller code | HTTP status only (section 6) |
| unknown or unresolved attempt → cleared | Rolando, with `factory reconcile` showing GitHub matches and the session list link | Rolando's recorded **clearing evidence** (section 6.1), not just a decision |
| candidate found → ready for review | controller code | branch/PR marker match, head SHA, required checks status |
| attempt cap / single-writer lock | controller code | ledger counters (ENG-146) |

The controller makes no model calls; one worker session performs the implementation (that session itself makes many model calls). Optional later helpers (contract drafting, review summaries) may use a model but can never write to the ledger or change a transition.

## 4. Capability table

Legend: **Observed** = seen in this account on 2026-10-07; **Documented** = official docs say so (quote/URL); **Unsupported** = docs say it cannot be done, or no such capability exists in public docs; **Untested** = plausible but not yet seen. "Pending A" = Appendix A will turn it into Observed.

| Capability | Status | Evidence | What the factory does |
|---|---|---|---|
| **Launch** (fire one run with run-specific context) | Documented; **Observed** 2026-10-08 (3 fires, all `200`; session ids are `cse_…` and URLs `https://claude.ai/code/cse_…`, not `session_…` as the docs show) | `POST https://api.anthropic.com/v1/claude_code/routines/{trig_…}/fire`, `Authorization: Bearer sk-ant-oat01-…`, `anthropic-version: 2023-06-01`, body `{"text": "…"}` up to 65,536 chars; returns `claude_code_session_id` and `claude_code_session_url` "once the session is created" ([fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire)). Rate limits: 30 fires/hour per routine, 100 API fires/hour per account, `429` with `Retry-After` | Controller sends contract ID, digest, attempt and the contract body as `text`. Sends both `anthropic-version` and `anthropic-beta: experimental-cc-routine-2026-04-01` (the two doc pages disagree on whether the beta header is required; the fire page says it is accepted either way) |
| Launch idempotency | Unsupported | "There is no idempotency key. If a webhook caller retries, the endpoint creates multiple sessions" (fire API) | No automatic retry of any fire that might have created a session (section 6) |
| **Outcome evidence** | Documented (weak); GitHub path **Observed** 2026-10-08 (draft PR #4 by `rnavarrete-factory-bot` on `claude/probe-1-a1`, 21 s after the fire, base = `main` head) | Run list "green status … means the session started and exited without an infrastructure error. It does not mean the task in your prompt succeeded" ([Routines](https://code.claude.com/docs/en/routines)). Work is pushed to a `claude/`-prefixed branch unless the prompt says otherwise. No public API for session status, events or result | Controller reads GitHub only: marker branch (named explicitly in the prompt; sessions otherwise get a random `claude/` branch), marker PR and its GitHub author, head SHA, required checks. Commit author is always `Claude <noreply@anthropic.com>` and is not identity evidence. Worker self-report and run-list status are never evidence (ADR 0001 section 4) |
| **Uncertain launch** (timeout, 500, 503, connection reset) | Unsupported to resolve by API | Fire token cannot read; no public list-sessions API; 500 says "Retry with exponential backoff", which would risk duplicates | Record `launch-outcome-unknown`, stop, and require Rolando's manual reconciliation (section 6) |
| **Stop monitoring** | Documented | Closing the tab "doesn't stop the session"; pausing a routine stops future runs ("Paused routines … don't run") | Controller simply stops polling GitHub; the ledger keeps the run open until reconciled. Pausing the routine is the kill switch for **new** runs; the fire API then returns `400` "the routine is paused" |
| **Stop an active worker** | Unsupported as a guaranteed hard stop by API; **Observed** 2026-10-08: stopping the run from the UI halted its pushes (last push 00:35:53Z, none in the following 2 min of a loop that pushed every ~17 s). One observation, UI only | No public cancel/interrupt API. UI offers archive and delete; archived sessions "cannot accept new messages", delete "permanently removes the session and its data" ([web docs](https://code.claude.com/docs/en/claude-code-on-the-web)). Whether archive halts in-flight tool execution is not documented. Internal session tools (interrupt/archive) exist in this project's sessions but are not a public contract and are not relied on | **Supervised execution, auto-fix off.** Containment comes from the boundaries: one repo, branch-only pushes, ruleset with empty bypass list (ENG-142), no connectors, narrow network, no secrets in the environment. Rolando may stop/archive a run from the UI as a best-effort stop. The 2026-10-08 result is **observational only**: no further pushes is not proof that the session was cancelled, so it never clears an attempt by itself (section 6.1) |
| **Usage visibility** | Documented (aggregate only); per-run Unsupported | "Routines draw down subscription usage … See your current consumption at claude.ai/settings/usage"; no separate VM charge; hourly run caps, no documented daily cap; no per-run token/cost figure for routines. Observed: session metadata here exposes a five-hour rate-limit window status | ENG-138 budgets by **run count and wall time per task**, not tokens. Rolando reads the usage page weekly. Over-limit runs are rejected unless usage credits are on: keep credits **off** for the factory account so budgets fail closed |
| **Local fallback** | Documented; Untested | `claude -p` headless "exits with code 0 on success and a non-zero code when the run fails"; `--output-format json` includes `total_cost_usd`; SIGTERM gives exit 143 ([headless](https://code.claude.com/docs/en/headless)) | An **unqualified** fallback candidate (section 8). Exit code and per-run cost are documented; isolation and a reliable stop are not established |
| **What carries over to the cloud** | Documented; **Observed** 2026-10-08 on the factory account | Loaded: repo `CLAUDE.md`, repo `.claude/{rules,skills,agents,commands}`, repo `.claude/settings.json` and `.mcp.json` (single-repo sessions), claude.ai-enabled skills, org managed settings. **Not** loaded: plugins a repo enables under `enabledPlugins`, user `~/.claude` content, user/local MCP servers, user hooks ([cloud environments](https://code.claude.com/docs/en/cloud-environments)). Observed: a fresh cloud session's `~/.claude` holds only harness-provided files, no user settings | Everything the worker needs lives **in the pilot repo** (ENG-139 `CLAUDE.md`, ENG-141 setup). No plugin or user-config dependency. Appendix A run: `~/.claude` held only harness files plus one `synced/` plugin and skill directory from the factory claude.ai account; MCP servers `github` and `claude-code-remote`; claude.ai skills (anthropic-skills set) loaded; repo `CLAUDE.md` present and followed (the report used its "Not done" format). The factory account should switch off claude.ai skills and synced plugins it does not need (ENG-181) |
| **Autonomy / permissions** | Documented | Routines have "no permission-mode picker"; connectors' tools usable "including writes, without asking" | Boundaries, not prompts, limit the worker (ADR 0001 section 9 item 3) |
| **Auto-fix** | Observed risk; **not verified** in the 2026-10-08 run | Auto-fix "responds to" CI failures and review comments and pushes repairs ([auto-fix](https://code.claude.com/docs/en/claude-code-on-the-web#auto-fix-pull-requests)). This project's own cloud sessions show `autofix_on_pr_create: true`. Whether routine sessions inherit it is not documented | **Pass condition, not an observation.** Auto-fix must be confirmed off on the factory routine's PRs before the runtime counts as qualified, and checked and recorded on every pilot PR (ENG-182 first run, ENG-158). If it is on and cannot be turned off, pilot dispatch stops |

## 5. Selected repo and process setup (criterion 3)

- **Process:** Appendix A, run once by Rolando with the controller's fire script, proves the actual cloud path: routine created in the web UI, API token generated, fire from the workstation, run clones the repo, follows the repo's `CLAUDE.md`, pushes a marker branch and opens a draft PR, controller finds it on GitHub.
- **Repo:** the pilot repo `rNavarrete/factory-pilot-demo` (public, chosen in ENG-137 / ADR 0003), as soon as Rolando has created it. No throwaway repo. The probe only pushes `claude/probe-*` branches and draft PRs, which are closed and deleted afterwards; it changes nothing on `main`, does not count as a pilot task, and runs under the factory claude.ai account (linked to the bot GitHub identity from ENG-183), so it exercises the real path end to end.
- **No carry-over assumed:** the probe's prompt asks the worker to report its `~/.claude` contents, plugins and MCP servers, and the repo files it can see. Pass = repo cloned at `main`'s head SHA; no `~/.claude` user content, no user plugins, no local or user MCP servers. Skills enabled on claude.ai do load in cloud sessions by design: list and record them, and the factory account disables any it does not need. Repo-config loading (the pilot's `CLAUDE.md`) is not testable yet because ENG-139 writes that file; the first ENG-145 run checks it, so that one row stays Untested until then.
- Result is recorded in section 10 with date, Claude Code version, session URL and Rolando's active minutes.

## 6. Launch outcomes and reconciliation

| Fire result | Ledger state | Next |
|---|---|---|
| `200` with `type: routine_fire` and a session id | `launched` (session id + URL stored) | On `factory status` / `factory reconcile`, query GitHub for the marker. No daemon polls |
| `400`, `401`, `403`, `404`, `429` | `not-launched` (documented codes that create no session; `400` includes "the routine is paused") | **Stop.** A re-fire is a failed-launch exception (ADR 0001 section 4) and needs Rolando's recorded decision. It reuses the same attempt number because no session was created; for `429`, not before `Retry-After` |
| `500`, `503`, any other status, `200` without a session id, timeout (180 s), connection reset, interrupted call, unparseable body | `launch-outcome-unknown` | **Stop.** No retry. Controller prints the routine's run-list URL and any GitHub marker matches. Rolando records one of: `launched <session-url>`, `not-launched`, or `duplicate <urls>` (then archives extras). Until he does, the single-writer lock stays held and nothing else dispatches |

Rule from ENG-136: unsupported cancellation means supervised execution and auto-fix off, not a claimed hard stop; uncertain launches require manual reconciliation. Attempt caps block **new** launches only.

### 6.1 When another worker may start

A task stays **blocked** while any earlier attempt on it is unresolved: `launch-outcome-unknown`, `launched` without a recorded end, or `duplicate`. The controller allows the next fire only when Rolando has recorded, for every earlier attempt, one of these **clearing records**:

1. **Definitive no-session response:** the fire itself returned a documented no-session code (`400`, `401`, `403`, `404` or `429`, section 6). Nothing else proves a launch never happened. A session missing from the run list after a timeout or `5xx` is **not** proof, because the original request may still create its session after the check, and a new fire would start a second worker.
2. **Confirmed completion or termination:** the session is found in the run list (or by its marker branch or PR) and shows as finished, or stopped and archived, with its URL recorded.
3. **Verified loss of GitHub write capability:** the bot account's write access to the pilot repo is removed (collaborator removed, or its Claude GitHub App authorization revoked) **and** a check confirms the bot can no longer push, recorded with how it was checked. Deleting or pausing the routine does **not** count: that only kills the fire token, which starts sessions. A running session pushes through the GitHub proxy with separate credentials ([fire-token scope](https://platform.claude.com/docs/en/api/claude-code/routines-fire#authentication), [GitHub proxy](https://code.claude.com/docs/en/cloud-environments#github-proxy)).

Evidence for rule 3 (ENG-180 switch-off test, 2026-10-08): removing the bot as collaborator made an **already running** factory session's push fail at 01:12Z with `403` ("Claude doesn't have GitHub access to rNavarrete/factory-pilot-demo"). The session's own proxy blocks the collaborator-permission API, so the confirming check is done from outside the worker: the GitHub collaborators page or `gh api repos/<repo>/collaborators` run by Rolando or this controller. Whether a surviving old session regains push access when the bot is re-added was **not tested** (the long-lived session was deleted before ENG-180's restore step), so the restore rule below is a precaution, not a tested result.

**Restoring access after rule 3.** Because the bot's access is shared by every session acting as the bot, restoring it would also restore a surviving old worker. So access is restored only after every unresolved attempt has a rule 2 record. If an old session can never be found, the factory stays stopped for that repo. Rolando may instead record an explicit **exception** (ADR 0001 section 4) accepting that an unknown session may still exist. The attempt then stays marked `unresolved-accepted` (absence is recorded as evidence, never as certainty), and every later candidate on that task is checked for pushes from it.

A manual note records what Rolando checked; it never turns "not seen" into "did not happen".

These do **not** clear an attempt on their own: a PR appearing, a quiet branch, minutes without pushes, or a UI stop with no recorded session state. A **repair authorization** from Rolando also does not clear an unresolved earlier attempt; he must record a clearing record first.

Evidence is bound to a commit: any new push to a candidate branch invalidates CI results, verifier reports and approvals tied to an earlier commit (ADR 0001 section 5.2). The pilot measures Rolando's minutes spent on reconciliation separately (ENG-162), because that time could erase the benefit of running workers off his machine.

## 7. What the controller is not

No general orchestrator, queue, scheduler, webhook listener, GitHub App, process-pack framework, or Grok dependency (Grok stays optional until ENG-173). The pilot uses GitHub's own review UI and the operator CLI as the approval interface (ADR 0001 section 7).

## 8. Exit path if Routines fail qualification

Triggers for leaving Routines: Appendix A fails on the cloud path; the fire API is withdrawn or changes incompatibly; ENG-183 cannot give the cloud worker a separate identity (cloud rollout then pauses, and an adapter below is one candidate "supported alternative"); or containment in section 4 cannot be shown by ENG-142/181.

Fallback, in order of preference, behind the same `RuntimeAdapter` interface ENG-185 defines (so ledger, approval and dispatch code do not change):

1. **Local headless adapter (unqualified candidate):** controller runs `claude -p --output-format json` on Rolando's machine under the factory account's login. Documented gains: exit code and per-run cost. Not established: isolation and a reliable stop. A git worktree only separates checkouts and does not isolate the worker from the workstation; a worker running with Rolando's user permissions could read the controller's ledger and his credentials however the files are placed; and terminating the parent process does not prove its child processes stopped ([git worktree](https://git-scm.com/docs/git-worktree), [subprocess](https://docs.python.org/3/library/subprocess.html)). **Before adoption** it needs: an isolated environment (separate OS user or VM/container) with no access to the ledger, Keychain or personal credentials, and a demonstrated termination test that kills the whole process tree. Not built now.
2. **GitHub Actions adapter** (only if 1 is unworkable): a `workflow_dispatch` job running headless Claude Code under the bot identity. Gains hosted execution and cancellable runs; costs API-key billing and an Actions secret, which needs ENG-138 to re-budget.

Every adapter must still commit and open PRs as the separate bot identity, never as Rolando, so his PR approval stays required and meaningful. Switching adapters is a new ADR (or an amendment Rolando signs) because it changes where code runs.

## 9. Version assumptions and requalification triggers

Assumptions as of 2026-10-07:

- Routines research preview; fire endpoint at `/v1/claude_code/routines/{id}/fire`, `anthropic-version: 2023-06-01`. **Docs disagree on the beta header:** the API reference says `anthropic-beta: experimental-cc-routine-2026-04-01` is optional ([headers](https://platform.claude.com/docs/en/api/claude-code/routines-fire#headers)), while the Routines guide still describes shipping behind dated beta headers with the two previous versions kept working ([guide](https://code.claude.com/docs/en/routines#trigger-a-routine)). The controller sends both headers, but the two-version grace is **not** relied on as a guarantee: any header or shape change is a requalification trigger (item 1 below).
- Token: per routine, write-only, regenerate revokes the old one, no management API. Observed 2026-10-08: after the routine was deleted, a fire with its token returned `401 authentication_error` "OAuth access token has been revoked", so deleting a routine kills its token.
- Limits: 30 fires/hour per routine, 100 API fires/hour per account; no documented daily cap; no documented max run duration (Bash commands 2 min default, 10 min max unless raised via `BASH_MAX_TIMEOUT_MS`, background commands up to 30 more minutes, idle VMs pause after a few minutes).
- Claude Code CLI 2.1.293 in cloud sessions (observed). Relevant gates: 2.1.213+ fired prompt treated as the task; 2.1.227+ run history via `/schedule`.
- Cloud environment: Ubuntu 24.04, setup script as root, network levels None/Trusted/Full/Custom; GitHub proxy blocks tag pushes and branch deletion only; GraphQL blocked so `gh api` REST is needed.
- Account has a single `Default` environment and no routines (observed).
- Observed by ENG-183's live test (2026-10-07): commits in a cloud session carry git author `Claude <noreply@anthropic.com>`, while GitHub actions (push, PR) run as the bot account; sessions start on an auto-assigned `claude/<random>` branch. So **bot identity means the authenticated GitHub actor (who pushes) and the PR author**, both `rnavarrete-factory-bot`; the git commit-author field is a different, unauthenticated value and is never identity evidence. The controller identifies a run by the explicitly named marker branch and the PR author, never by commit author or default branch name.
- Routine model: whatever is selected in the routine form ("Claude uses the selected model on every run"); recorded in section 10 at qualification.

Requalify when any of these happens. Review the relevant changes first, then rerun **only the affected Appendix A steps** and update section 4:

1. A new dated beta header ships, or the fire page's request/response shape, error codes or limits change.
2. Routines leave research preview, or the docs add a session read/cancel API (this would upgrade "outcome evidence" and "stop an active worker").
3. **Any** Claude Code release (patch releases included) affecting routines, authentication, configuration loading, permissions or execution behaviour. Versions 2.1.213, 2.1.227 and 2.1.293 already changed routine behaviour, so patch releases count.
4. Rolando's plan, the factory account's plan, or usage-credit settings change.
5. The pilot repo, its environment, network level or connectors change.
6. Two `launch-outcome-unknown` results in the pilot, or any run that pushed outside its marker branch.
7. The routine's selected model changes or is retired.
8. Every 30 days during the pilot: a **documentation check** only (about 10 minutes comparing the Routines, fire API, cloud-environment and auto-fix pages against section 9). It triggers a rerun only if it finds a change listed above. This is separate from a full Appendix A rerun.

## 10. Qualification results

| Date | Who | Claude Code version / model | Repo | Result | Session URL | Rolando active min |
|---|---|---|---|---|---|---|
| 2026-10-07 | Claude (read-only observation in this project's own cloud session) | 2.1.293 | none | One `Default` environment (trusted network); no routines; user `~/.claude` not present in session; `autofix_on_pr_create: true` on this session | n/a | 0 |
| 2026-10-08 | Rolando (clicks) + Claude (fired from this project's cloud session with the token, instead of the Mac/Keychain path) | 2.1.293 / routine default model | rNavarrete/factory-pilot-demo @ 30a4ae2 | **Pass.** Factory-account routine `factory-probe`, env `factory-probe` (network None), no connectors. Fire → draft PR by the bot on the named marker branch in 21 s; worker started on an auto-named `claude/zealous-bohr-…` branch and switched as told; used GitHub MCP, not `gh`; no user config leaked; UI stop halted a running loop. **Auto-fix: not verified** (not checked before the probe PRs were closed), so qualification is **conditional** until ENG-182's first factory routine PR shows auto-fix off. Stop test: UI stop at about 00:36Z, last push 00:35:53Z, none in the next 2 min (observational, section 4). Evidence: PRs [#4](https://github.com/rNavarrete/factory-pilot-demo/pull/4), [#5](https://github.com/rNavarrete/factory-pilot-demo/pull/5), [#7](https://github.com/rNavarrete/factory-pilot-demo/pull/7) (closed); worker report pasted in the ENG-136 thread 2026-10-08 00:36Z; token revoked check 01:18Z (`401` "OAuth access token has been revoked") | cse_018ST9r9SkDfTfHfAqrbwkot, cse_01QXjP2NFVAezGGc6zHnynVq, cse_011F6TQ9dBTnaenT4d1TpnCQ | ~25 |

Note: from this project, Claude cannot create a fresh-session routine ("create_new_session_on_fire is not supported for routines created from a private project"), so the live run needs Rolando's hands.

## 11. Identity go/no-go (ENG-183)

**Status (2026-10-07): GO.** The technical criteria below all pass. Rolando decided at 23:59Z not to wait on Anthropic support before proceeding ("we don't have time to check in with ant, we'll just have to keep going, if they alert us then we will stop"). The open question of whether a second personal Pro account is acceptable is an **accepted risk owned by Rolando**. If Anthropic objects or restricts the factory account, cloud rollout stops at once and a supported alternative is chosen (section 8). Required PR approval is not weakened in any case.

| Criterion | Result | Evidence |
|---|---|---|
| Account/plan arrangement and cost | Second personal claude.ai **Pro** account, monthly; first invoice $21.32 ($20 + tax) on 2026-10-07. Anthropic's Consumer Terms neither allow nor forbid a second account; not asked of support (Rolando's accepted risk, see Status). | Billing screenshot; planning/eng-183/support-question.md |
| Ruleset availability | Available free: pilot repo is public, so rulesets, required reviews and environment required reviewers work on GitHub Free. Re-qualify if the repo ever becomes private. | GitHub docs: About rulesets; Deployments and environments |
| Bot GitHub identity | Machine account `rnavarrete-factory-bot` (id 339357449), allowed by GitHub ToS §B.3 (one free machine account per person). 2FA on, recovery codes in password manager (Rolando). | GitHub user search; Rolando |
| Access path, bot as collaborator | Bot is a **write** collaborator on rNavarrete/factory-pilot-demo (not admin); factory account's claude.ai GitHub connection authorized as the bot through the Claude GitHub App (installed by rNavarrete). | GitHub collaborators API, 2026-10-07 23:09Z |
| Cloud session identity and PR author | **Pass.** In a session from the factory account, `gh api user` and `get_me` = rnavarrete-factory-bot; draft PR #3 author = rnavarrete-factory-bot. Git commit author is the session default `Claude <noreply@anthropic.com>`, which is not an identity control. | PR #3 (closed) |
| Another private repo inaccessible | **Pass (one-way).** Read of private `factory-canary-private` failed; push to public `factory-canary-public` refused 403. Both refusals came from the session proxy's per-session repo scoping, which is itself a boundary; the bot was also never granted those repos. | Session output 2026-10-07 23:53Z |
| No admin or bypass | Bot role = write. No rulesets exist yet; ENG-142 adds them with an empty bypass list and re-tests bot merge/settings attempts. | Collaborators API |
| No personal credentials on the factory account | Separate email and GitHub identity; no connectors or secrets (Rolando). Payment card is Rolando's (accepted: not a login credential). | Rolando |

Notes for later tickets:
- ENG-142: Claude-authored commits carry `Claude <noreply@anthropic.com>` as git author; rulesets must not rely on commit author.
- ENG-180: the access path is the Claude GitHub App + bot user authorization; scope = the app installation's repo selection on rNavarrete ∩ the bot's collaborator access.
- Routine sessions start on an auto-assigned `claude/<random>` branch; ENG-182/176 must instruct the marker branch `claude/<task>-a<n>` explicitly.

## 12. Acceptance criteria trace (ENG-136)

| Criterion | Where answered |
|---|---|
| Exact runtime/account, controller host, durable storage, secret storage; critical transitions make no LLM calls | Section 3 and its transitions table |
| Capability table: observed / documented / unsupported / untested for launch, outcome evidence, uncertain launch, stopping monitoring, stopping an active worker, usage visibility, local fallback | Section 4 |
| One selected repo/process setup works in the actual cloud path; local plugins/user config not assumed to carry over | Section 5 and Appendix A; **passed 2026-10-08 except auto-fix**, which ENG-182's first run must confirm (section 10) |
| ENG-183 records identity go/no-go here after this ticket closes; ENG-180/181/182 stay blocked on it | Section 11 (GO, ENG-183 Done); Linear relations unchanged |
| Unsupported cancellation means supervised execution and auto-fix off; uncertain launches need manual reconciliation | Section 4 rows "Stop an active worker", "Uncertain launch" and "Auto-fix" (pass condition); sections 6 and 6.1 |
| Version assumptions and requalification triggers recorded | Section 9 |

---

## Appendix A: live cloud-path qualification (Rolando, about 20 minutes)

Claude prepares the probe repo contents and the fire script; Rolando does the clicks only he can do. Script: [`fire_probe.py`](../eng-136/fire_probe.py).

1. **Repo.** Use `rNavarrete/factory-pilot-demo` once you have created it for ENG-137. Make sure the Claude GitHub App can see it (the routine form lists only repos it can reach). Nothing to add to the repo.
2. **Environment.** At claude.ai/code create environment `factory-probe`: network **None** (or Custom with only the registry the repo needs), no environment variables, no setup script.
3. **Routine.** At claude.ai/code/routines create `factory-probe`: that one repo, environment `factory-probe`, remove all connectors, no schedule, no GitHub triggers. Prompt: the text in [`probe-routine-prompt.md`](../eng-136/probe-routine-prompt.md).
4. **Token.** Edit the routine, Add another trigger, API, Generate token. Save it straight into Keychain: `security add-generic-password -s software-factory -a routine-token/<trig_id> -w` (paste at the prompt). Note the `trig_…` id.
5. **Fire.** `python3 fire_probe.py <trig_id>`. It reads the token from Keychain, fires once with a probe task ID, prints the HTTP status and session URL, and never retries.
6. **Check.** In the session: reported HEAD matches `main`; no `~/.claude` user content, user plugins or local MCP servers (claude.ai-enabled skills may appear; note them); branch `claude/probe-1-a1` pushed; draft PR titled `[probe-1 a1 …]`. Then `python3 fire_probe.py --find <owner/repo> probe-1` lists the marker branch/PR from GitHub.
7. **Stop test.** Fire once more with `--task probe-2 --long` (the run pushes a commit every ~15 s for ~10 minutes). Once the first loop commit appears on `claude/probe-2-a1`, note its SHA and time, archive the session from the UI, then watch the branch for 5 minutes for further pushes. Observational only: no further pushes is not proof of cancellation.
8. **Auto-fix (pass condition).** Before closing the probe PR, confirm auto-fix is off on it and record that. If it is on, turn it off and record how; the probe does not pass until this is done.
9. **Clean up.** Revoke the API token in the routine's trigger modal, then delete the probe routine, delete the Keychain item, close the two probe PRs and delete the `claude/probe-*` branches (Claude can do this once the repo is attached to its session). Tell Claude the minutes spent.

## Sign-off record

Sign-off applies to this file as it stands above this record.

- Version signed: `df1a33b27b88b5dcd04887f2f8c45f270e3b6ba6e6ebbe7be0a71eacfd404b77` (sha256 of this file above the sign-off record)
- Architecture: approved ("okay signing off", ENG-136 thread, 2026-10-08 01:31Z)
- Runtime qualification: conditional; rollout blocked until ENG-182's first factory routine PR shows auto-fix off
- Rolando's active time on ENG-136: about 40 minutes (probe ~25, two reviews ~15)
- Post-sign-off evidence correction (2026-10-08 01:34Z, no decision changed): section 6.1 rule 3 evidence now says the restore behaviour was not tested. The version hash above covers the text before this correction.

### Repository copy note

This copy differs from the signed planning-folder text in one place: the factory account's email address was removed from the section 11 "Account/plan arrangement and cost" row, because this repository is public (ENG-185, 2026-10-08). Links to `../eng-136/` point to the project's planning folder and do not resolve here.
