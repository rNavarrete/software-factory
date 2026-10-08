# software-factory

The controller for Rolando's personal software factory. It hands one approved task at a time to a cloud worker, keeps a record of every launch, and checks the result on GitHub. It never approves, merges or releases anything; Rolando does.

The controller is a small Python 3.11+ command-line program that uses only the standard library. Rolando runs it by hand on his Mac. It makes no AI model calls. Its record is a SQLite file at `~/.software-factory/`, outside every git checkout, and the worker's start key lives in macOS Keychain. The design is in [docs/adr/0002-runtime-controller-identity.md](docs/adr/0002-runtime-controller-identity.md).

The worker runs against a separate pilot repo, [rNavarrete/factory-pilot-demo](https://github.com/rNavarrete/factory-pilot-demo). Workers never get access to this repo.

## Develop

```sh
python3 -m unittest discover -s tests -t .   # tests
pip install ruff==0.15.12                    # lint tool, dev only
ruff check . && ruff format --check .        # lint
```

CI (`.github/workflows/ci.yml`) runs both on every pull request, on Python 3.11 and 3.13. The required check is `checks`.

## Layout and who owns what

Each directory belongs to one ticket so parallel work never edits the same files. Edit only the paths your ticket owns.

| Path | Owner | What goes there |
|---|---|---|
| `controller/interfaces.py` | ENG-185 | Shared types: `TaskId`, `AttemptId`, `RunId`, `ContractDigest`, `LedgerStore`, `RuntimeAdapter` and launch results |
| `controller/contract/`, `schema/` | ENG-144 | Contract format, canonical bytes and digest. Exposes `validate(contract, digest) -> list[str]` and the canonical digest function, which the routine adapter calls before every launch |
| `controller/ledger/` | ENG-147 | SQLite `LedgerStore`, event kinds, backups |
| `controller/approval/` | ENG-151 | Approval records |
| `controller/attempts/` | ENG-146 | Attempt and fire caps, holds |
| `controller/recovery/` | ENG-153 | Reconciling interrupted and unknown launches |
| `controller/adapter/fake.py` | ENG-185 | Scripted test adapter (success, rate rejection, lost response) |
| `controller/adapter/routine.py` | ENG-182 | The real cloud Routine adapter |
| `controller/dispatch/` | ENG-176 | The dispatch path |
| `controller/cli.py`, `controller/__main__.py`, `[project.scripts]` in `pyproject.toml` | ENG-176 | The `factory` command. Other tickets expose plain functions; ENG-176 wires them to subcommands (`dispatch`, `status`, `reconcile`, `hold`, `resume`, `snapshot`) |
| `verify/` | ENG-156, ENG-157 | Criterion verifier and assertion mapper |
| `controller/loop/collect.py`, `verify/review.py` | ENG-145 | Reading a worker PR's evidence from GitHub, and the independent mapper's review comment |
| `controller/loop/` | ENG-145 | `python3 -m controller.loop run <contract.json>`: one task from contract to a PR ready for Rolando's review |
| `tasks/samples/` | ENG-145 | The sample task contracts for the first full runs |
| `redteam/` | ENG-158 | Red-team checks |
| `docs/adr/0001-operating-model.md` | ENG-134 | Operating model and human authority |
| `docs/adr/0002-runtime-controller-identity.md` | ENG-136 (ENG-183 owns section 11) | Runtime and controller |
| `docs/adr/0003-pilot-and-value-thresholds.md` | ENG-137 | Pilot and value thresholds |
| `docs/governance-map.md` | ENG-135 | Controls and how each is tested |
| `docs/limits.md` | ENG-138 | Attempt limits and usage budgets |
| `tests/` | each ticket adds its own `test_<area>.py` | |
| `.github/`, `pyproject.toml`, `README.md`, `controller/__init__.py`, `controller/adapter/__init__.py` | ENG-185 | CI, lint config, this file, package markers |

The signed documents under `docs/` are copied from the project's planning folder. They are unchanged except for one email address removed from ADR 0002 (noted at its end). Each sign-off record holds a hash of the signed text, so any other edit needs a new sign-off from Rolando.

## Changing the shared interfaces

`controller/interfaces.py` is what every other package builds on. Don't change it as a side effect of another ticket. If you need a change, open a separate PR that touches only `interfaces.py` and its tests, title it `interfaces: <what>`, and say which tickets it affects. Adding a new optional field or method is fine; renaming or removing one needs every caller fixed in the same PR.

Fixed conventions the types encode:

- Marker branch: `claude/<task>-a<n>`. PR title prefix: `[<task> a<n> <digest12>]`. These are how the controller finds a run's work on GitHub.
- PR body line `Contract-Digest: <64 hex>` (`ContractDigest.pr_body_line`), which the pilot's CI reads and the controller checks against the title marker.
- A launch has exactly three outcomes: `launched`, `not-launched` (only HTTP 400, 401, 403, 404, 429) and `launch-outcome-unknown` (everything else). Adapters never retry and never raise for network failures.
- The ledger is append-only. `append` writes one or more events in one transaction, only while the single-writer lock is held. That lock just stops two controller processes writing at once; whether a new dispatch is allowed is decided from ledger events.
- Shared parsers for the markers live here too (`AttemptId.from_branch`, `AttemptId.from_pr_title`, `ContractDigest.from_pr_body`) so no ticket writes its own regex.
