"""Check every control in the governance map against its evidence (ENG-163).

The governance map (docs/governance-map.md) lists every control the pilot
relies on, its class and the negative test that shows it refusing the unsafe
case. ``controls.CONTROLS`` says, for each one, where that negative test was
observed:

- ``tests``: unittest ids, run here and now. A test that is missing, errors
  or fails holds the control.
- ``redteam``: red-team case ids (redteam/cases.py), run here and now. Only a
  case blocked for its expected reason counts; a live case not run yet holds.
- ``records``: what was observed live (a GitHub setting, a worker session, a
  release run), with where it is written down, the date and the exact
  configuration or commit it ran against. Records can't be re-run from here,
  so each one must say all four.
- ``pending``: what still has to be observed. Anything here holds the check.

The rules (governance map section 6):

- The class must match the map exactly, so a control can't quietly change
  from "Advisory" to "Code" here (or back) without the map changing too.
- A required control needs evidence of the kind its class implies: a run
  test or case for Code and Detective, a live record for Platform and
  Boundary, either for a Human gate. Nothing pending.
- An Advisory control needs no evidence, only to be labeled Advisory in the
  map, with a note on what is recorded about it. It never counts as holding
  anything.
- Every ID in the map appears exactly once, and nothing else does except the
  blocked-mode checks (IDs starting ``M-``).

``Report.all_observed`` is true only when nothing holds. It is a statement
about evidence, not an approval: whether to go ahead stays Rolando's.
"""

from __future__ import annotations

import io
import re
import unittest
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass

CLASSES = ("Code", "Platform", "Boundary", "Human gate", "Detective", "Advisory", "Unsupported")
_ID_RE = re.compile(r"^G-[A-G]\d{1,2}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MODE_PREFIX = "M-"
_UNSTATED = re.compile(r"^\s*(not stated|unknown|n/?a|none|-)?\s*$", re.I)
_NOT_SEEN = re.compile(
    r"\b(pending|not (yet )?(tested|observed|run|done|captured|attempted|demonstrated)"
    r"|never (run|tried|attempted|observed)|could not be read)\b",
    re.I,
)


@dataclass(frozen=True)
class Record:
    """Something observed live, outside this repo's tests."""

    what: str
    """What was seen, in plain words (including the refusal itself)."""
    where: str
    """Where it is written down: a project planning file or an https link."""
    observed_on: str
    """YYYY-MM-DD."""
    revision: str
    """The configuration, commit or ruleset it ran against."""

    def problems(self) -> list[str]:
        out = [f"record has no {k}" for k in ("what", "where", "revision") if not getattr(self, k)]
        if not _DATE_RE.match(self.observed_on):
            out.append(f"record date {self.observed_on!r} is not YYYY-MM-DD")
        if _UNSTATED.search(self.revision):
            out.append(f"record names no configuration ({self.revision!r})")
        if _NOT_SEEN.search(self.what):
            out.append(f"record says it was not observed: {self.what[:80]!r}")
        return out


@dataclass(frozen=True)
class Control:
    id: str
    classes: tuple[str, ...]
    """As the map's Class column names them, in order."""
    tests: tuple[str, ...] = ()
    redteam: tuple[str, ...] = ()
    records: tuple[Record, ...] = ()
    pending: str = ""
    """What still has to be observed. Holds the check while set."""
    note: str = ""
    """For Advisory rows: what is recorded about it, and that nothing relies on it."""

    @property
    def advisory(self) -> bool:
        """Only advisory. A row that is Advisory plus another class (auto-fix:
        advisory, plus a detective audit) is required for its other class."""
        return set(self.classes) == {"Advisory"}

    @property
    def required(self) -> bool:
        return not self.advisory


@dataclass(frozen=True)
class Row:
    control: Control
    holds: tuple[str, ...]
    evidence: tuple[str, ...]

    @property
    def observed(self) -> bool:
        return not self.holds


@dataclass(frozen=True)
class Report:
    rows: tuple[Row, ...]
    problems: tuple[str, ...]
    """Problems with the audit itself (the map and the control list disagree)."""

    @property
    def holds(self) -> tuple[str, ...]:
        out = list(self.problems)
        for r in self.rows:
            out += [f"{r.control.id}: {h}" for h in r.holds]
        return tuple(out)

    @property
    def all_observed(self) -> bool:
        return not self.holds


# --- the map ------------------------------------------------------------------------


def map_classes(text: str) -> dict[str, tuple[str, ...]]:
    """Each control's classes, read from the governance map's section 3 tables.

    Raises ValueError on a row it can't read or an ID listed twice, so a
    reformatted map stops the audit instead of silently shrinking it."""
    out: dict[str, tuple[str, ...]] = {}
    section = ""
    for line in text.splitlines():
        if line.startswith("## "):
            section = line
        if not section.startswith("## 3.") or not line.lstrip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if cells[0] == "ID" or set("".join(cells)) <= set("-: "):
            continue  # a header or separator row
        cid = cells[0]
        if not _ID_RE.match(cid):
            raise ValueError(f"can't read map row {cid!r}: not a control ID")
        # The signed map's G-F6 row has its owner and mechanism in one cell
        # (6 columns); its class is then the 4th. Any other short row is unreadable.
        class_at = 4 if len(cells) == 7 else 3 if (len(cells), cid) == (6, "G-F6") else None
        if class_at is None:
            raise ValueError(f"can't read map row {cid!r}: {len(cells)} columns, not 7")
        if cid in out:
            raise ValueError(f"{cid} is listed twice in the map")
        out[cid] = _classes(cells[class_at])
    if not out:
        raise ValueError("no controls found in the map's section 3")
    return out


_CLASS_NOTES = {
    # Prose the signed map puts after a class name, accepted as written.
    "Code on the recorded snapshot; the usage reading itself is human": "Code",
    "Human gate in the pilot; Code once the verifier is a program": "Human gate",
}


def _classes(cell: str) -> tuple[str, ...]:
    """'Detective (Code once the verifier is a program) + Human gate' -> (Detective, Human gate).

    Each part must be exactly a class name once parenthetical notes (and an
    em-dash note) are removed; anything else stops the audit."""
    plain = re.sub(r"\([^)]*\)", "", cell.replace("**", ""))
    plain = re.sub(r"—.*$", "", plain)
    found = []
    for part in plain.split("+"):
        part = " ".join(part.split())
        part = _CLASS_NOTES.get(part, part)
        if part not in CLASSES:
            raise ValueError(f"unknown class {part!r} in {cell!r}")
        found.append(part)
    return tuple(found)


# --- running the evidence -----------------------------------------------------------

Runner = Callable[[Sequence[str]], Mapping[str, str | None]]
"""Runs ids; returns, for each, None when it passed or why it didn't."""


def run_unittests(ids: Sequence[str]) -> dict[str, str | None]:
    """Run the named unittest ids once each. A name that doesn't load fails."""
    out: dict[str, str | None] = {}
    loader = unittest.TestLoader()
    for test_id in dict.fromkeys(ids):
        try:
            suite = loader.loadTestsFromName(test_id)
        except (AttributeError, ImportError, ValueError) as e:
            out[test_id] = f"does not exist ({type(e).__name__})"
            continue
        if loader.errors:
            out[test_id] = "does not exist"
            loader.errors.clear()
            continue
        found = list(_flatten(suite))
        if len(found) != 1:
            out[test_id] = f"names {len(found)} tests, not exactly one"
            continue
        if found[0].id() != test_id or not test_id.rsplit(".", 1)[-1].startswith(
            loader.testMethodPrefix
        ):
            out[test_id] = "is not a test method"
            continue
        result = unittest.TextTestRunner(stream=io.StringIO(), verbosity=0).run(suite)
        if result.errors or result.failures:
            out[test_id] = "fails" if result.failures else "errors"
        elif result.skipped:
            out[test_id] = "was skipped"
        elif result.expectedFailures or result.unexpectedSuccesses:
            out[test_id] = "is marked as an expected failure"
        elif not result.wasSuccessful() or result.testsRun != 1:
            out[test_id] = "did not run as one passing test"
        else:
            out[test_id] = None
    return out


def _flatten(suite):
    for t in suite:
        if isinstance(t, unittest.TestSuite):
            yield from _flatten(t)
        else:
            yield t


def run_redteam(ids: Sequence[str]) -> dict[str, str | None]:
    from redteam.cases import CASES, LIVE_CASES, run_case

    by_id = {c.id: c for c in (*CASES, *LIVE_CASES)}
    out: dict[str, str | None] = {}
    for case_id in dict.fromkeys(ids):
        case = by_id.get(case_id)
        if case is None:
            out[case_id] = "no such red-team case"
            continue
        r = run_case(case)
        out[case_id] = None if r.ok else r.result.value
    return out


# --- the audit ----------------------------------------------------------------------


def run_audit(
    controls: Iterable[Control],
    map_text: str,
    *,
    run_tests: Runner = run_unittests,
    run_cases: Runner = run_redteam,
) -> Report:
    controls = tuple(controls)
    problems: list[str] = []
    try:
        mapped = map_classes(map_text)
    except ValueError as e:
        return Report((), (f"governance map: {e}",))

    seen: dict[str, int] = {}
    for c in controls:
        seen[c.id] = seen.get(c.id, 0) + 1
    for cid, n in seen.items():
        if n > 1:
            problems.append(f"{cid} appears {n} times in the control list")
        if cid not in mapped and not cid.startswith(MODE_PREFIX):
            problems.append(f"{cid} is not a control in the governance map")
    problems += [f"{cid} has no entry in the control list" for cid in mapped if cid not in seen]

    tests = run_tests([t for c in controls for t in c.tests])
    cases = run_cases([k for c in controls for k in c.redteam])
    rows = tuple(_row(c, mapped.get(c.id), tests, cases) for c in controls)
    return Report(rows, tuple(problems))


def _row(c: Control, mapped: tuple[str, ...] | None, tests, cases) -> Row:
    holds: list[str] = []
    evidence: list[str] = []
    if mapped is not None and mapped != c.classes:
        holds.append(f"class {' + '.join(c.classes)} differs from the map's {' + '.join(mapped)}")
    for t in c.tests:
        why = tests.get(t, "was not run")
        if why is None:
            evidence.append(f"test {t}: passed")
        else:
            holds.append(f"test {t} {why}")
    for k in c.redteam:
        why = cases.get(k, "was not run")
        if why is None:
            evidence.append(f"red-team case {k}: blocked")
        else:
            holds.append(f"red-team case {k}: {why}")
    for r in c.records:
        bad = r.problems()
        if bad:
            holds += bad
        else:
            evidence.append(f"observed {r.observed_on} ({r.revision}): {r.what} [{r.where}]")
    if c.pending:
        holds.append(f"not observed yet: {c.pending}")

    if "Advisory" in c.classes and not c.note:
        holds.append("labeled advisory, but says nothing about what is recorded for it")
    if c.advisory:
        return Row(c, tuple(dict.fromkeys(holds)), tuple(evidence))

    run = [t for t in c.tests if tests.get(t, "") is None] + [
        k for k in c.redteam if cases.get(k, "") is None
    ]
    live = [r for r in c.records if not r.problems()]
    kinds = set(c.classes)
    if kinds & {"Code", "Detective"} and not run:
        holds.append("no passing test or red-team case shows it refusing the unsafe case")
    if kinds & {"Platform", "Boundary"} and not live:
        holds.append("no live record shows the setting or boundary holding")
    if (
        "Human gate" in kinds
        and not kinds & {"Code", "Detective", "Platform", "Boundary"}
        and not run
        and not live
    ):
        holds.append("no test or record shows the loop stopping until his decision exists")
    if "Unsupported" in kinds:
        holds.append("unsupported: the mode that needs it stays blocked")
    return Row(c, tuple(dict.fromkeys(holds)), tuple(evidence))


# --- output -------------------------------------------------------------------------


def render(report: Report, *, revision: str = "") -> str:
    lines = ["# Full control check", ""]
    if revision:
        lines += [f"Factory commit checked: `{revision}`", ""]
    if report.all_observed:
        lines += ["**Every control is observed.** Whether to go ahead stays Rolando's call.", ""]
    else:
        lines += [
            f"**Not every control is observed yet: {len(report.holds)} thing(s) hold.**",
            "",
        ]
    if report.problems:
        lines += ["## Problems with the check itself", ""]
        lines += [f"- {p}" for p in report.problems] + [""]
    lines += [
        "| Control | Class | Result | Evidence or what holds it |",
        "|---|---|---|---|",
    ]
    for r in report.rows:
        c = r.control
        if c.advisory:
            result = "advisory (not relied on)" if r.observed else "held"
        else:
            result = "observed" if r.observed else "**held**"
        detail = "; ".join(r.holds) if r.holds else ""
        shown = list(r.evidence)
        if c.note:
            shown.insert(0, c.note)
        if detail:
            detail = (
                "HOLDS: " + detail + (" — evidence so far: " + "; ".join(shown) if shown else "")
            )
        else:
            detail = "; ".join(shown)
        lines.append(f"| {c.id} | {' + '.join(c.classes)} | {result} | {_cell(detail)} |")
    return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")
