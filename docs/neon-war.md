# Neon War on the factory

The factory works on two repositories: the pilot (`rNavarrete/factory-pilot-demo`) and the game-week game (`rNavarrete/neon-war`). This page says what is shared, what is separate, and the steps left before the first game ticket can start.

## What is separate and what is shared

| | Pilot | Neon War |
|---|---|---|
| Worker routine | `factory-worker` (`trig_01CHWbQ267i1CMLGUym1kGd9`) | The same routine, with neon-war added to its repositories (Rolando's choice, 2026-10-26) |
| Start key secret | `routine-token` | the same `routine-token` |
| Linear project | Factory Pilot Demo (only its listed tickets) | Neon War: any ticket Rolando moves to Todo, except the status ticket ENG-495 and anything labelled `baseline` |
| Starts per rolling 7 days (reviews included) | 12, alert at 9 | 24, alert at 18 ([limits.md amendment](limits.md#amendment-2026-10-26-a-weekly-cap-per-repository)) |
| Workers running at once | one, shared | one, shared |
| Plan-usage stop (75% weekly), hold, rate-limit wait | shared | shared |

The repository table is `controller/targets.py`. The onboarding file can only map a Linear project to a repository listed there, with exactly the routine listed for it. Every attempt records its contract's repository when it is reserved, and recovery, CI evidence and the Codex review all read that repository, never a default.

Neon War's own rules for workers are in its `CLAUDE.md`. Its CI has the same `check`, `control-change`, `check-selftest` and `verified` jobs as the pilot, so the base-commit choice and evidence collection work unchanged.

Sharing the routine means a worker started for either repository holds credentials for both. The factory still sends each contract only for its own repository and its own onboarded tickets, and recovery and review read only that repository. A separate routine later is one line in `controller/targets.py` plus its own start-key secret (`neon-war-routine-token`).

## Steps before the first ticket

The onboarding and drafting entries are already in `deploy/pilot/`. What is left is on accounts:

1. **Add neon-war to `factory-worker`** on the factory account (the routine's repositories).
2. **GitHub access.** Give `rnavarrete-factory-bot` write access to neon-war. Widen the service's read-only `github-token` to neon-war too. Check the reviewer still can't write there: `python3 -m controller.review qualify-identity <login> --repo rNavarrete/neon-war`.
3. **Protect `main`** on neon-war with a ruleset requiring the `verified` check, as on the pilot.
4. **Deploy**, then move a ticket to Todo once docs/GAME.md for Milestone 1 and its art are on neon-war's main.

### Onboarding entry (as shipped)

The entry has no `issues` list (removed 2026-10-27, so new game tickets need no factory change): Rolando's move to Todo is what approves a ticket, as for every project.

```json
{
  "linear_project_id": "08057006-6d52-4743-a43c-dfc2370c7a12",
  "name": "Neon War",
  "repository": "rNavarrete/neon-war",
  "routine_id": "trig_01CHWbQ267i1CMLGUym1kGd9",
  "allowed_actions": ["modify-files", "add-files", "add-tests"],
  "checks": ["npm run typecheck", "npm test", "npm run build"],
  "max_attempts": 2,
  "repair_allowance": 1,
  "repair_allowance_since": "2026-10-26T06:00:00+00:00",
  "status_issue_id": "ab11b765-6762-4137-b4d4-257086103d88",
  "skip_labels": ["baseline"],
  "protected_paths": [
    ".github/", ".claude/", "CLAUDE.md", "scripts/", "public/art/",
    "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "playwright.config.ts"
  ]
}
```

### Drafting entry (as shipped)

`max_criteria` is 15, not the pilot's 6 (2026-10-27): the Brownout tickets each have 8 to 15 small criteria, one per rule or number, and at 6 the factory would have stopped every one of them to propose a split.

```json
{
  "linear_project_id": "08057006-6d52-4743-a43c-dfc2370c7a12",
  "writable_paths": ["src/**", "tests/**", "index.html", "README.md", "docs/**"],
  "protected_paths": [
    ".github/**", ".claude/**", "CLAUDE.md", "scripts/**", "public/art/**",
    "package.json", "package-lock.json", "eslint.config.js", ".prettierrc.json",
    ".prettierignore", "tsconfig.json", "vite.config.ts", "playwright.config.ts", ".nvmrc"
  ],
  "verification_commands": ["npm run typecheck", "npm test", "npm run build"],
  "test_command": "npm test",
  "escalate_to": "rNavarrete",
  "max_criteria": 15,
  "max_ticket_chars": 8000,
  "base": {"branch": "main", "workflow_path": ".github/workflows/ci.yml", "job": "verified", "lookback": 20},
  "worker": ["node 22", "npm ci"]
}
```

## Known limits

- **"All checks pass" lines.** Each Brownout ticket ends with one. No single test can show it, so since 2026-10-27 the verifier counts the trusted check's overall result (`npm run check` in CI) as its evidence, and the Codex review may answer "cannot tell" for it (`verify.criteria.checks_only`). A line that adds anything beyond a short list of check names is an ordinary criterion and still needs a linked test.
- **"(unit tests)" marks a criterion as a test check.** The drafter reads words like "list" or "screen" as a built-page check that only Rolando can observe. A criterion ending "(unit tests)" is drafted as an automated check whatever else it says (drafter `rules/v2`, 2026-10-27).
- **Helpers with an object return type.** Until 2026-10-27 the verifier read `function f(): { a: T } {` as opening the body at the type, so the helper's own `return` looked like an exit before every later test, and every link in the file was "unknown" (ENG-502's first review).

- **Playwright tests aren't linked evidence yet.** The verifier and the Codex review link criteria only to Vitest files (`tests/**/*.test.ts`, run by `npm test`). Neon War's Playwright suite still runs in CI inside `npm run check`, so a failing browser test fails the PR, but a criterion about what the player sees is checked by Rolando's observation (the CI screenshots), not by a linked Playwright assertion. Teaching the verifier about `tests/browser/*.spec.ts` is a follow-up.
- **One worker at a time.** A neon-war ticket waits while a pilot worker holds the lane, and the other way round. The lane frees itself once a PR has gone quiet for 30 minutes.
