# Neon War on the factory

The factory works on two repositories: the pilot (`rNavarrete/factory-pilot-demo`) and the game-week game (`rNavarrete/neon-war`). This page says what is shared, what is separate, and the steps left before the first game ticket can start.

## What is separate and what is shared

| | Pilot | Neon War |
|---|---|---|
| Worker routine | `factory-worker` (`trig_01CHWbQ267i1CMLGUym1kGd9`) | Its own routine, neon-war attached only |
| Start key secret | `routine-token` | `neon-war-routine-token` |
| Starts per rolling 7 days (reviews included) | 12, alert at 9 | 24, alert at 18 ([limits.md amendment](limits.md#amendment-2026-10-26-a-weekly-cap-per-repository)) |
| Workers running at once | one, shared | one, shared |
| Plan-usage stop (75% weekly), hold, rate-limit wait | shared | shared |

The repository table is `controller/targets.py`. The onboarding file can only map a Linear project to a repository listed there, with exactly the routine listed for it. Every attempt records its contract's repository when it is reserved, and recovery, CI evidence and the Codex review all read that repository, never a default.

Neon War's own rules for workers are in its `CLAUDE.md`. Its CI has the same `check`, `control-change`, `check-selftest` and `verified` jobs as the pilot, so the base-commit choice and evidence collection work unchanged.

## Steps before the first ticket

1. **Create the routine** on the factory account: neon-war attached (nothing else), the same prompt as `factory-worker` (`controller/adapter/routine_prompt.md`), a Node 22 environment with the npm registry allowed. Put its `trig_…` id in `NEON_WAR_ROUTINE` in `controller/targets.py` (a reviewed one-line change).
2. **Store its start key** with `fly secrets set FACTORY_NEON_WAR_ROUTINE_TOKEN=…` from a hidden prompt, as `go-live.sh` does for the pilot's.
3. **GitHub access.** Give `rnavarrete-factory-bot` write access to neon-war. Widen the service's read-only `github-token` to neon-war too. Check the reviewer still can't write there: `python3 -m controller.review qualify-identity <login> --repo rNavarrete/neon-war`.
4. **Protect `main`** on neon-war with a ruleset requiring the `verified` check, as on the pilot.
5. **Linear.** Make a Neon War project. Add its entry to `deploy/pilot/onboarding.json` and `deploy/pilot/drafting.json` (below), list the ticket keys in `issues`, then deploy.

### Onboarding entry

```json
{
  "linear_project_id": "<Neon War Linear project id>",
  "name": "Neon War",
  "repository": "rNavarrete/neon-war",
  "routine_id": "<neon-war trig_ id>",
  "allowed_actions": ["modify-files", "add-files", "add-tests"],
  "checks": ["npm run typecheck", "npm test", "npm run build"],
  "max_attempts": 2,
  "repair_allowance": 1,
  "repair_allowance_since": "<deploy time, ISO with zone>",
  "status_issue_id": "<a Neon War status ticket id>",
  "issues": ["<the 8 ticket keys>"],
  "protected_paths": [
    ".github/", ".claude/", "CLAUDE.md", "scripts/", "public/art/",
    "package.json", "package-lock.json", "tsconfig.json", "vite.config.ts", "playwright.config.ts"
  ]
}
```

### Drafting entry

```json
{
  "linear_project_id": "<Neon War Linear project id>",
  "writable_paths": ["src/**", "tests/**", "index.html", "README.md", "docs/**"],
  "protected_paths": [
    ".github/**", ".claude/**", "CLAUDE.md", "scripts/**", "public/art/**",
    "package.json", "package-lock.json", "eslint.config.js", ".prettierrc.json",
    ".prettierignore", "tsconfig.json", "vite.config.ts", "playwright.config.ts", ".nvmrc"
  ],
  "verification_commands": ["npm run typecheck", "npm test", "npm run build"],
  "test_command": "npm test",
  "escalate_to": "rNavarrete",
  "max_criteria": 6,
  "max_ticket_chars": 8000,
  "base": {"branch": "main", "workflow_path": ".github/workflows/ci.yml", "job": "verified", "lookback": 20},
  "worker": ["node 22", "npm ci"]
}
```

## Known limits

- **Playwright tests aren't linked evidence yet.** The verifier and the Codex review link criteria only to Vitest files (`tests/**/*.test.ts`, run by `npm test`). Neon War's Playwright suite still runs in CI inside `npm run check`, so a failing browser test fails the PR, but a criterion about what the player sees is checked by Rolando's observation (the CI screenshots), not by a linked Playwright assertion. Teaching the verifier about `tests/browser/*.spec.ts` is a follow-up.
- **One worker at a time.** A neon-war ticket waits while a pilot worker holds the lane, and the other way round. The lane frees itself once a PR has gone quiet for 30 minutes.
