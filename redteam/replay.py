"""Re-run the offline bypass cases on records from a real delivery (ENG-158).

The offline cases each change one thing in a made-up honest run
(``redteam.fixtures``). This module applies that same change to the records a
real worker PR produced, as the evidence collector read them from GitHub, and
checks the pipeline still stops it for the same reason.

How a change is carried over: ``fixtures.honest()`` and the case's own scenario
are compared field by field. Whatever the case changed (a result's commit, a
link's mapper, a record removed or added, a contract field) is made to the
matching real record, with every made-up name swapped for the real one: the
fixture's head commit becomes the real head, its digest the approved digest,
its test names the real mapped tests, and so on. Values the case invented
(an older commit, another workflow, the worker's login) stay as they are.

A case that rewrites the made-up test file's text, or swaps in a precomputed
report, has no real counterpart and is reported as not replayable: those stay
offline-only, and the report says so rather than counting them.

Rolando's own decisions (his observations of by-hand criteria and his looks at
flags) live in his signed ledger, not on GitHub. Where the real baseline needs
them, ``stand_in_decisions`` supplies them, each labelled as a stand-in, so the
honest real run comes out ready and a blocked case means something.

``python3 -m redteam.replay <contract.json> <pr-number>`` collects one pilot
PR through ``controller.loop.collect`` (``gh api`` under the caller's login),
runs every replayable case on it and prints the summary. It reads GitHub only;
it never writes, fires, approves or merges.
"""

from __future__ import annotations

import dataclasses
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from enum import Enum

from controller import contract as contracts
from controller.interfaces import ContractDigest
from redteam import cases as rc
from redteam import fixtures as fx
from verify.assertions import Clearance
from verify.criteria import Observation, Verdict

STAND_IN = "Stand-in for Rolando's own signed record, which lives in his ledger, not on GitHub."


class NotReplayable(Exception):
    """The case's change has no counterpart in the real records."""


class Replayed(Enum):
    BLOCKED = "blocked"
    GOT_THROUGH = "got through"
    WRONG_REASON = "stopped, but not by the expected control"
    NOT_REPLAYABLE = "offline only (no real counterpart)"
    ERROR = "error"


@dataclass(frozen=True)
class ReplayResult:
    case: rc.Case
    result: Replayed
    detail: str
    offline: rc.Result
    """What the same case gives on the made-up records, for comparison."""

    @property
    def same_as_offline(self) -> bool:
        if self.result is Replayed.NOT_REPLAYABLE:
            return True
        return self.result.value == self.offline.value


# --- The real baseline --------------------------------------------------------------


def real_scenario(
    contract: Mapping[str, object],
    approved: ContractDigest,
    collected,
    review,
    *,
    observations=(),
    clearances=(),
) -> fx.Scenario:
    """The scenario a real PR gives: what the collector read, the independent
    review comment's mapping, and Rolando's decisions (or stand-ins)."""
    if collected.candidate is None:
        raise ValueError("the PR could not be read well enough to describe a candidate")
    return fx.Scenario(
        contract=contract,
        approved=approved,
        candidate=collected.candidate,
        results=collected.results,
        observations=tuple(observations),
        claims=collected.claims,
        links=review.links,
        sources=collected.sources,
        proofs=review.proofs,
        limits=review.limits,
        control_change=collected.control_change,
        clearances=tuple(clearances),
    )


def stand_in_decisions(s: fx.Scenario) -> fx.Scenario:
    """``s`` with a stand-in for each decision only Rolando can make: an
    observation of every by-hand criterion, and a look at every open flag."""
    c = s.candidate
    observable = [
        a["id"]
        for a in s.contract["acceptance_criteria"]
        if a["evidence"]["type"] in ("observable-behavior", "human-review")
    ]
    have = {o.criterion for o in s.observations}
    observations = tuple(s.observations) + tuple(
        Observation(
            criterion=cid,
            commit=c.head_commit,
            base_commit=c.base_commit,
            observer=fx.REVIEWER,
            verdict=Verdict.PASS,
            seen=f"{STAND_IN} Saw the behavior the contract describes.",
            limitations="Stand-in: nothing was observed for this replay.",
        )
        for cid in observable
        if cid not in have
    )
    s = s.but(observations=observations)
    flags = fx.evaluate(s).assertions.open_flags
    clearances = tuple(s.clearances) + tuple(
        Clearance(
            flag=f.key,
            contract_digest=str(s.approved),
            commit=c.head_commit,
            base_commit=c.base_commit,
            by=fx.REVIEWER,
            note=STAND_IN,
        )
        for f in flags
    )
    return s.but(clearances=clearances)


# --- Carrying a case's change over ----------------------------------------------------


def _names(real: fx.Scenario) -> list[tuple[str, str]]:
    """Made-up value -> real value, longest first, for every name a case can carry."""
    h = fx.honest()
    pairs: list[tuple[str, str]] = [
        (h.candidate.pr_title, real.candidate.pr_title),
        (h.candidate.pr_body, real.candidate.pr_body),
        (fx.HEAD, real.candidate.head_commit),
        (fx.MAIN, real.candidate.base_commit),
        (str(fx.DIGEST), str(real.approved)),
        (h.candidate.branch, real.candidate.branch),
        (str(fx.CONTRACT["task_id"]), str(real.contract["task_id"])),
    ]
    if fx.BASE != fx.MAIN:
        pairs.append((fx.BASE, real.candidate.merge_base))
    pairs.append((fx.DIGEST.short, real.approved.short))
    by_criterion = {lk.criterion: lk for lk in real.links}
    for cid, test, assertion in (
        ("ac1", fx.AC1_TEST, fx.AC1_ASSERT),
        ("ac2", fx.AC2_TEST, fx.AC2_ASSERT),
    ):
        if cid in by_criterion:
            pairs += [(test, by_criterion[cid].test), (assertion, by_criterion[cid].assertion)]
    test_file = _real_test_file(real)
    if test_file:
        pairs.append((fx.TEST_FILE, test_file))
    urls = [r.url for r in real.results if r.url]
    if urls:
        pairs.append((fx.CI_URL, urls[0]))
    if real.control_change is not None and real.control_change.url:
        pairs.append((fx.CC_URL, real.control_change.url))
    proof_urls = [p.url for p in real.proofs if p.url]
    if proof_urls:
        pairs.append((fx.PROOF_URL, proof_urls[0]))
    pairs = [(a, b) for a, b in pairs if a and b is not None]
    return sorted(pairs, key=lambda p: -len(p[0]))


def _real_test_file(real: fx.Scenario) -> str | None:
    paths = {lk.path for lk in real.links}
    return next(iter(paths)) if len(paths) == 1 else None


def _swap(value: object, names: Sequence[tuple[str, str]]) -> object:
    if isinstance(value, str):
        for made_up, actual in names:
            if value == made_up:
                return actual
        for made_up, actual in names:
            if len(made_up) >= 8:
                value = value.replace(made_up, actual)
        return value
    if isinstance(value, ContractDigest):
        return ContractDigest(str(_swap(str(value), names)))
    if isinstance(value, tuple):
        return tuple(_swap(v, names) for v in value)
    if isinstance(value, list):
        return [_swap(v, names) for v in value]
    if isinstance(value, Mapping):
        return {k: _swap(v, names) for k, v in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return replace(
            value,
            **{f.name: _swap(getattr(value, f.name), names) for f in dataclasses.fields(value)},
        )
    return value


def _text_edit(before: str, after: str, real: str, names) -> str:
    """The same edit as ``before`` -> ``after``, made to ``real``."""
    if after.startswith(before):
        return real + str(_swap(after[len(before) :], names))
    if after.endswith(before):
        return str(_swap(after[: len(after) - len(before)], names)) + real
    return str(_swap(after, names))


def _record_key(field: str, record: object, honest_head: str) -> object:
    if field == "results":
        return record.command
    if field in ("observations", "links", "proofs", "limits"):
        return record.criterion
    if field == "sources":
        return "head" if record.commit == honest_head else "base"
    return None  # clearances and claims: every real record


def _matches(field: str, key: object, record: object, real: fx.Scenario) -> bool:
    if key is None:
        return True
    if field == "sources":
        test_file = _real_test_file(real)
        commit = real.candidate.head_commit if key == "head" else real.candidate.merge_base
        return record.path == test_file and record.commit == commit
    return _record_key(field, record, real.candidate.head_commit) == key


def _carry_record(field, h_rec, m_rec, real_recs, real, names):
    """Apply the attribute changes h_rec -> m_rec to every matching real record."""
    key = _record_key(field, h_rec, fx.HEAD)
    targets = [i for i, r in enumerate(real_recs) if _matches(field, key, r, real)]
    if not targets:
        raise NotReplayable(f"no real {field[:-1]} matches the changed one ({key})")
    changed = {
        f.name: getattr(m_rec, f.name)
        for f in dataclasses.fields(h_rec)
        if getattr(h_rec, f.name) != getattr(m_rec, f.name)
    }
    if field == "sources" and changed.get("text") is not None:
        raise NotReplayable("it edits the made-up test file's text")
    out = list(real_recs)
    for i in targets:
        new = {}
        for name, value in changed.items():
            old = getattr(h_rec, name)
            if isinstance(value, str) and isinstance(old, str):
                new[name] = _text_edit(old, value, getattr(out[i], name), names)
            else:
                new[name] = _swap(value, names)
        out[i] = replace(out[i], **new)
    return out


def _carry_tuple(field, h_val, m_val, real_val, real, names):
    h_val, m_val, out = tuple(h_val), tuple(m_val), list(real_val)
    if len(h_val) == len(m_val):
        for h_rec, m_rec in zip(h_val, m_val, strict=True):
            if h_rec != m_rec:
                out = _carry_record(field, h_rec, m_rec, out, real, names)
        return tuple(out)
    removed = [r for r in h_val if r not in m_val]
    added = [r for r in m_val if r not in h_val]
    for h_rec in removed:
        key = _record_key(field, h_rec, fx.HEAD)
        keep = [r for r in out if not _matches(field, key, r, real)]
        if len(keep) == len(out):
            raise NotReplayable(f"no real {field[:-1]} matches the removed one ({key})")
        out = keep
    for m_rec in added:
        if field == "sources" and m_rec.text is not None:
            raise NotReplayable("it adds made-up test file text")
        out.append(_swap(m_rec, names))
    return tuple(out)


def carry_over(made: fx.Scenario, real: fx.Scenario) -> fx.Scenario:
    """The change ``made`` makes to the made-up honest run, made to ``real``."""
    h = fx.honest()
    names = _names(real)
    changes: dict[str, object] = {}
    for f in dataclasses.fields(fx.Scenario):
        hv, mv, rv = getattr(h, f.name), getattr(made, f.name), getattr(real, f.name)
        if hv == mv:
            continue
        if f.name == "verification":
            raise NotReplayable("it swaps in a precomputed report")
        if f.name == "contract":
            edited = dict(rv)
            for k in set(hv) | set(mv):
                if hv.get(k) != mv.get(k):
                    if k not in mv:
                        edited.pop(k, None)
                    elif k == "permitted_paths":
                        added = [p for p in mv[k] if p not in hv.get(k, ())]
                        edited[k] = [*rv.get(k, ()), *added]
                    else:
                        edited[k] = _swap(mv[k], names)
            changes["contract"] = edited
        elif f.name == "approved":
            if mv == contracts.digest(made.contract):
                changes["approved"] = None  # recomputed below, from the edited real contract
            else:
                changes["approved"] = _swap(mv, names)
        elif f.name == "candidate":
            new = {}
            for cf in dataclasses.fields(hv):
                a, b = getattr(hv, cf.name), getattr(mv, cf.name)
                if a == b:
                    continue
                r = getattr(rv, cf.name)
                if cf.name == "changed_paths" and not b:
                    new[cf.name] = ()
                elif cf.name == "changed_paths":
                    gone = [p for p in a if p not in b]
                    added = [p for p in b if p not in a]
                    new[cf.name] = tuple(p for p in r if p not in gone) + tuple(added)
                elif isinstance(a, str) and isinstance(b, str):
                    new[cf.name] = _text_edit(a, b, r, names)
                else:
                    new[cf.name] = _swap(b, names)
            changes["candidate"] = replace(rv, **new)
        elif f.name == "control_change":
            if mv is None or hv is None or rv is None:
                changes["control_change"] = _swap(mv, names)
            else:
                new = {
                    cf.name: _swap(getattr(mv, cf.name), names)
                    for cf in dataclasses.fields(hv)
                    if getattr(hv, cf.name) != getattr(mv, cf.name)
                }
                changes["control_change"] = replace(rv, **new)
        elif isinstance(hv, tuple):
            changes[f.name] = _carry_tuple(f.name, hv, mv, rv, real, names)
        else:
            changes[f.name] = mv
    out = real.but(**changes)
    if changes.get("approved", 0) is None:
        out = out.but(approved=contracts.digest(out.contract))
    return out


# --- Running the cases ------------------------------------------------------------------


def replayable(case: rc.Case) -> bool:
    return case.check is not None and hasattr(case.check, "make")


def _expect(case: rc.Case, real: fx.Scenario) -> tuple[str, ...]:
    """The case's expected reasons, with made-up names swapped for real ones."""
    commits = {
        fx.HEAD: real.candidate.head_commit,
        fx.MAIN: real.candidate.base_commit,
        fx.BASE: real.candidate.merge_base,
    }
    budget = (fx.CONTRACT["attempt_budget"], real.contract["attempt_budget"])
    out = []
    for rx in case.check.expect:
        for made_up, actual in _names(real):
            esc = re.escape(made_up)
            if esc in rx:
                rx = rx.replace(esc, re.escape(actual))
        # A shortened made-up commit ("aaaa") stands for the real one's start.
        for made_up, actual in commits.items():
            if len(set(made_up)) == 1:
                rx = re.sub(f"{made_up[0]}{{4,40}}", lambda m, a=actual: a[: len(m[0])], rx)
        rx = rx.replace(f"budget of {budget[0]}", f"budget of {budget[1]}")
        out.append(rx)
    return tuple(out)


def replay_case(case: rc.Case, real: fx.Scenario, offline: rc.Result) -> ReplayResult:
    if not replayable(case):
        return ReplayResult(case, Replayed.NOT_REPLAYABLE, "not a change to records", offline)
    try:
        scenario = carry_over(case.check.make(), real)
        out = fx.evaluate(scenario)
    except NotReplayable as e:
        return ReplayResult(case, Replayed.NOT_REPLAYABLE, str(e), offline)
    except Exception as e:  # a crash is never a pass
        return ReplayResult(case, Replayed.ERROR, f"{type(e).__name__}: {e}", offline)
    if out.ready:
        return ReplayResult(case, Replayed.GOT_THROUGH, "the pipeline said ready", offline)
    expect = _expect(case, real)
    missing = [rx for rx in expect if not re.search(rx, out.text)]
    if missing:
        first = out.blockers[0] if out.blockers else "(no blocker)"
        return ReplayResult(
            case,
            Replayed.WRONG_REASON,
            f"no reason matched {missing!r}; first blocker: {first}",
            offline,
        )
    hit = next((ln for ln in out.text.splitlines() if expect and re.search(expect[0], ln)), "")
    return ReplayResult(case, Replayed.BLOCKED, hit or out.blockers[0], offline)


def replay_all(real: fx.Scenario, cases: Sequence[rc.Case] = rc.CASES) -> tuple[ReplayResult, ...]:
    baseline = fx.evaluate(real)
    if not baseline.ready:
        raise ValueError(
            "the real baseline is not ready, so a blocked case would mean nothing: "
            + "; ".join(baseline.blockers)
        )
    offline = {r.case.id: r.result for r in rc.run_all(tuple(cases))}
    return tuple(replay_case(c, real, offline[c.id]) for c in cases)


def summary(results: Sequence[ReplayResult], label: str) -> str:
    counts: dict[Replayed, int] = {}
    for r in results:
        counts[r.result] = counts.get(r.result, 0) + 1
    lines = [f"Offline cases re-run on {label}:"]
    lines += [f"  {k.value}: {counts.get(k, 0)}" for k in Replayed]
    differ = [r for r in results if not r.same_as_offline]
    lines.append(f"  differ from the offline run: {len(differ)}")
    for r in differ:
        lines.append(f"    {r.case.id}: {r.result.value} (offline: {r.offline.value}): {r.detail}")
    return "\n".join(lines)


def collect_real(contract_path: str, pr_number: int, api=None) -> tuple[fx.Scenario, str]:
    """Collect one pilot PR and build its real scenario, with stand-in decisions."""
    from controller.interfaces import AttemptId, TaskId
    from controller.loop import collect as k
    from verify.review import read_review

    with open(contract_path, encoding="utf-8") as f:
        contract = contracts.loads(f.read())
    approved = contracts.digest(contract)
    api = api or k.GhApi()
    attempt = AttemptId(TaskId(str(contract["task_id"])), 1)
    collected = k.collect(api, contract, approved, attempt, pr_number)
    if collected.ci is not None and collected.ci.base_sha is None and collected.candidate:
        # A merged PR's run no longer lists the PR, so GitHub stops saying which
        # base it ran on. Use the PR's recorded base; the collector still checks
        # it against the commits the run's own control-change report names.
        ci = replace(collected.ci, base_sha=collected.candidate.base_commit)
        results, control, problems = k._ci_evidence(
            api, k.PILOT_REPO, ci, collected.candidate.head_commit, approved
        )
        collected = replace(collected, ci=ci, results=results, control_change=control)
        if problems:
            raise ValueError("; ".join(problems))
    unusable = [p for p in collected.problems if not p.endswith("is closed.")]
    if unusable or collected.pending:
        raise ValueError("; ".join(unusable) or "CI has not finished for this PR")
    review = read_review(collected.comments, approved.value, collected.candidate)
    real = real_scenario(contract, approved, collected, review)
    return stand_in_decisions(real), collected.pr_url


def main(argv: Sequence[str], out: Callable[[str], None] = print) -> int:
    if len(argv) != 2 or not argv[1].isdigit():
        out("usage: python3 -m redteam.replay <contract.json> <pr-number>")
        return 2
    from controller.loop.collect import GitHubUnreadable

    try:
        real, url = collect_real(argv[0], int(argv[1]))
        results = replay_all(real)
    except (GitHubUnreadable, ValueError, OSError) as e:
        out(f"Could not replay: {e}")
        return 1
    out(summary(results, url))
    return 0 if all(r.same_as_offline for r in results) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
