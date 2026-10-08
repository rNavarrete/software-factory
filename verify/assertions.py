"""Map each approved acceptance criterion to the assertion that shows it (ENG-157).

ENG-156's verifier (``verify.criteria``) says whether each criterion's command
passed on the exact candidate. A passing ``npm test`` only says no test failed;
it does not say any test checks the criterion. This module answers the next
question for Rolando: for each criterion, which assertion in which test, on
which exact commit, shows it holds, and is there any reason to doubt that
assertion?

Every input is collected read-only by someone other than the worker, and every
piece of evidence names the approved contract digest and the exact revision it
is for, so evidence for an earlier contract or commit is never reused:

- ``Report``: ENG-156's report for the same candidate and contract. An
  automated criterion is only covered if its command passed there.
- ``AssertionLink``: a mapper (any login but the worker's) names the test and
  quotes the assertion that checks a criterion, and says why it does. The
  mapper can be Rolando or an independent read-only verifier session; the
  worker's own mapping (for example the PR body) is a claim, never a link.
- ``TestSource``: test files' text at the candidate commit and at its merge
  base, read from git. The module reads the text itself: the named test must
  exist and be registered unconditionally, and must contain the quoted
  assertion as a complete call that is not hidden in a comment or string.
- ``FailureProof``: the linked test, taken from the candidate, run against the
  base commit's product code. It shows the check detects the original failure.
  Where that is not practical (new behavior, where the test cannot even load
  without the new code), a ``FailureProofLimit`` records why.
- ``ControlChangeReport``: the pilot CI's control-change report for this exact
  revision. That CI job always shows green and only reports, so a flagged
  report is held here as a flag, never trusted as a pass.
- ``Clearance``: a policy reviewer (Rolando) saying he looked at one flag on
  this exact revision.

Each criterion is ``covered``, ``uncovered`` (nothing maps to it), ``unknown``
(something maps to it but cannot be confirmed) or ``failed``. Only covered
counts. Nothing can waive a gap: a clearance names a flag, never a criterion,
so the only way past an uncovered or unknown criterion is a revised contract
that Rolando approves again, which has a new digest, so all evidence must be
collected afresh.

Flags are things a person must look at before the result counts: control
changes; deleted, weakened or changed existing tests; changed imports, kept
apart from other changes outside tests (shared values, hooks, helpers) so that
clearing the import change an honest task makes never clears those; comments
that switch checks off (``@ts-nocheck``, ``eslint-disable``...) added in any
changed file whose text was supplied; assertions on fixtures rather than
product code; weak matchers and bounds; expected values computed by the code
under test or worked out with array methods; assertions that may not run; and
checks never shown to fail without the change. Each flag holds readiness
until a reviewer clears it on this exact revision.

The reading of test files is deliberately suspicious: anything it cannot
follow with confidence (shadowed or aliased test functions, runtime skips,
options objects that can skip tests, tests registered inside functions or
conditions, unparseable text) makes the link unknown rather than covered. It
is still a heuristic, not a JavaScript engine. A test that mirrors an incorrect
implementation in a way these checks cannot see is possible, which is why the
failure proof and Rolando's review exist. Known limits of the setup checks: a
patch made through a local alias of a global (``const A = Array; A[...] = ...``)
inside a new test is not seen, and a new suite with the same title as an old
one is read as the old one, so its fixtures count as setup.

The module only reads. It does no I/O, changes none of its inputs, and its
report has no way to approve, merge or release: ``ready`` means "ready for
Rolando to review". Standard library only.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum

from controller.contract import validate
from controller.interfaces import ContractDigest
from verify.criteria import (
    DEFAULT_POLICY,
    Candidate,
    Citation,
    Gate,
    Report,
    TrustPolicy,
    Verdict,
)

_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:-?[A-Za-z0-9]){0,38}(?:\[bot\])?$")
_TEST_NAME_RE = re.compile(r"\.(test|spec)\.[cm]?[jt]sx?$")
_SEP = " > "


def _commit(value: str, what: str) -> None:
    if not isinstance(value, str) or not _COMMIT_RE.fullmatch(value):
        raise ValueError(f"{what} must be a full 40-character lowercase commit id")


def _digest(value: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.fullmatch(value):
        raise ValueError("contract_digest must be 64 lowercase hex characters")


def _text(value: object, what: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"{what} must be text")


def _login_key(login: str) -> str | None:
    """A login compared ignoring case and a ``[bot]`` suffix; None if malformed."""
    if not isinstance(login, str) or not _LOGIN_RE.fullmatch(login):
        return None
    return login.lower().removesuffix("[bot]")


# --- Inputs ---------------------------------------------------------------------


@dataclass(frozen=True)
class TestSource:
    """One test file's text at one commit, read from git by the collector."""

    __test__ = False  # not a test class

    path: str
    commit: str
    text: str | None
    """None when the file does not exist at that commit."""

    def __post_init__(self) -> None:
        _commit(self.commit, "commit")
        _text(self.path, "path")
        if self.text is not None:
            _text(self.text, "text")


@dataclass(frozen=True)
class AssertionLink:
    """A mapper's statement that one assertion checks one criterion."""

    criterion: str
    path: str
    test: str
    """The test's full name: enclosing describe names and its own, joined by " > "."""
    assertion: str
    """The assertion as written in the test (whitespace may differ)."""
    contract_digest: str
    """The approved contract the mapper read the criterion from."""
    commit: str
    base_commit: str
    mapper: str
    """Who made the mapping. The worker's own mapping is never a link."""
    why: str
    """How the assertion shows the criterion's statement, in the mapper's words."""

    def __post_init__(self) -> None:
        _digest(self.contract_digest)
        _commit(self.commit, "commit")
        _commit(self.base_commit, "base_commit")
        for name in ("criterion", "path", "test", "assertion", "mapper", "why"):
            _text(getattr(self, name), name)


class ProofOutcome(Enum):
    FAILED_ASSERTION = "failed-assertion"
    """The linked assertion ran and failed: the check detects the original failure."""
    FAILED_ERROR = "failed-error"
    """The test failed before its assertion (missing export, type error)."""
    PASSED = "passed"
    """The test passed without the change: it does not detect the original failure."""


@dataclass(frozen=True)
class FailureProof:
    """The linked test from the candidate, run against the base's product code."""

    criterion: str
    path: str
    test: str
    contract_digest: str
    tests_commit: str
    """The candidate commit the test file was taken from."""
    code_commit: str
    """The product code it ran against: the candidate's merge base."""
    outcome: ProofOutcome
    by: str
    url: str
    """Where anyone can see the run or its log."""
    output_excerpt: str = ""

    def __post_init__(self) -> None:
        _digest(self.contract_digest)
        _commit(self.tests_commit, "tests_commit")
        _commit(self.code_commit, "code_commit")
        if not isinstance(self.outcome, ProofOutcome):
            raise ValueError("outcome must be a ProofOutcome")


@dataclass(frozen=True)
class FailureProofLimit:
    """Why showing the check fail without the change is not practical here."""

    criterion: str
    contract_digest: str
    commit: str
    base_commit: str
    by: str
    reason: str

    def __post_init__(self) -> None:
        _digest(self.contract_digest)
        _commit(self.commit, "commit")
        _commit(self.base_commit, "base_commit")


@dataclass(frozen=True)
class ControlChangeReport:
    """The pilot CI control-change job's report (``control-change.json``)."""

    head_commit: str
    base_commit: str
    flagged: bool
    reasons: tuple[str, ...]
    url: str
    repository: str
    """Read from GitHub's record of the run, not from the file."""
    workflow_path: str
    app: str

    def __post_init__(self) -> None:
        _commit(self.head_commit, "head_commit")
        _commit(self.base_commit, "base_commit")
        if not isinstance(self.flagged, bool):
            raise ValueError("flagged must be true or false")
        if isinstance(self.reasons, str):
            raise ValueError("reasons must be a list, not one string")
        object.__setattr__(self, "reasons", tuple(str(r) for r in self.reasons))

    @classmethod
    def from_json(
        cls,
        data: Mapping[str, object],
        *,
        url: str,
        repository: str,
        workflow_path: str,
        app: str,
    ) -> ControlChangeReport:
        """Read the pilot's ``control-change.json``. Where it came from is the caller's."""
        flagged = data.get("flagged")
        reasons = data.get("reasons")
        return cls(
            head_commit=data.get("headCommit") or data.get("head"),
            base_commit=data.get("baseCommit") or data.get("base"),
            # Anything but an explicit false is treated as flagged.
            flagged=flagged is not False,
            reasons=tuple(reasons) if isinstance(reasons, list) else (),
            url=url,
            repository=repository,
            workflow_path=workflow_path,
            app=app,
        )


@dataclass(frozen=True)
class Clearance:
    """A reviewer saying they looked at one flag on this exact revision."""

    flag: str
    """The flag's ``key``, exactly as the report shows it."""
    contract_digest: str
    commit: str
    base_commit: str
    by: str
    note: str
    """What they checked. A clearance with no note is ignored."""

    def __post_init__(self) -> None:
        _digest(self.contract_digest)
        _commit(self.commit, "commit")
        _commit(self.base_commit, "base_commit")


@dataclass(frozen=True)
class AssertionPolicy:
    trust: TrustPolicy = DEFAULT_POLICY
    """ENG-156's policy: worker logins, trusted workflow, plain-content paths."""
    reviewers: frozenset[str] = frozenset({"rNavarrete"})
    """Who may clear a flag (compared ignoring case). Never a worker login."""
    suites: tuple[tuple[str, str], ...] = (("npm test", r"tests/(?:[^/]+/)*[^/]+\.test\.ts"),)
    """Which test files each command runs, as (command, full-match regex). The
    pilot's ``vite.config.ts`` includes ``tests/**/*.test.ts``."""

    def _workers(self) -> set[str]:
        return {w.lower().removesuffix("[bot]") for w in self.trust.worker_logins}

    def independent(self, login: str) -> bool:
        """A well-formed login that is not the worker's in any spelling."""
        key = _login_key(login)
        return key is not None and key not in self._workers()

    def is_reviewer(self, login: str) -> bool:
        key = _login_key(login)
        return (
            self.independent(login)
            and not login.lower().endswith("[bot]")
            and key in {r.lower() for r in self.reviewers}
        )

    def suite_for(self, command: str) -> str | None:
        return next((rx for cmd, rx in self.suites if cmd == command), None)


DEFAULT_ASSERTION_POLICY = AssertionPolicy()


# --- Outputs --------------------------------------------------------------------


class Coverage(Enum):
    COVERED = "covered"
    UNCOVERED = "uncovered"
    """Nothing independent maps the criterion to an assertion or observation."""
    UNKNOWN = "unknown"
    """Something maps to it, but it cannot be confirmed on this revision."""
    FAILED = "failed"


class FlagKind(Enum):
    CONTROL_CHANGE = "control-change"
    DELETED_TEST = "deleted-test"
    WEAKENED_TEST = "weakened-test"
    CHANGED_TEST = "changed-test"
    CHANGED_SETUP = "changed-setup"
    SUPPRESSION = "check-suppression"
    FIXTURE_ONLY = "fixture-only"
    WEAK_ASSERTION = "weak-assertion"
    MIRRORS_CODE = "mirrors-code"
    MAY_NOT_RUN = "may-not-run"
    NO_FAILURE_PROOF = "no-failure-proof"
    PASSES_WITHOUT_CHANGE = "passes-without-change"


@dataclass(frozen=True)
class Flag:
    """Something a person must look at before the result counts."""

    kind: FlagKind
    subject: str
    detail: str
    criterion: str | None = None
    cleared_by: str | None = None
    note: str = ""

    @property
    def key(self) -> str:
        """What a clearance names. Every finding with this key is in ``detail``."""
        return f"{self.kind.value}:{self.subject}"


@dataclass(frozen=True)
class MappedAssertion:
    path: str
    test: str
    line: int
    assertion: str
    """The whole assertion as found in the source, not as the mapper quoted it."""
    commit: str
    mapper: str
    why: str


@dataclass(frozen=True)
class CriterionCoverage:
    criterion: str
    statement: str
    evidence_type: str
    status: Coverage
    commit: str
    """The exact commit the evidence is for."""
    assertions: tuple[MappedAssertion, ...] = ()
    observations: tuple[Citation, ...] = ()
    """For observed criteria: ENG-156's citations of what a reviewer saw."""
    limits: tuple[str, ...] = ()
    """What the evidence does not show."""
    reasons: tuple[str, ...] = ()
    """For anything but covered: what is missing or wrong."""


@dataclass(frozen=True)
class AssertionReport:
    contract_digest: str
    repository: str
    candidate_commit: str
    base_commit: str
    gates: tuple[Gate, ...]
    criteria: tuple[CriterionCoverage, ...]
    flags: tuple[Flag, ...]
    verification: Report
    ignored: tuple[str, ...] = ()

    @property
    def open_flags(self) -> tuple[Flag, ...]:
        return tuple(f for f in self.flags if f.cleared_by is None)

    @property
    def blockers(self) -> tuple[str, ...]:
        out = [f"{g.name}: {g.detail}" for g in self.gates if not g.ok]
        out += [f"criterion check: {b}" for b in self.verification.blockers]
        out += [
            f"{c.criterion} is {c.status.value}: {'; '.join(c.reasons)} "
            "(this cannot be cleared: fix the evidence on a new revision, or revise the "
            "contract and approve it again)"
            for c in self.criteria
            if c.status is not Coverage.COVERED
        ]
        out += [f"needs review ({f.key}): {f.detail}" for f in self.open_flags]
        if not self.criteria:
            out.append("no acceptance criteria were mapped")
        return tuple(out)

    @property
    def ready(self) -> bool:
        """Ready for Rolando's review. This is not an approval."""
        return not self.blockers

    def still_current(self, candidate: Candidate) -> list[str]:
        """Why this report no longer describes ``candidate``; empty if it still does."""
        reasons = []
        if candidate.head_commit != self.candidate_commit:
            reasons.append(
                f"candidate moved from {self.candidate_commit} to {candidate.head_commit}"
            )
        if candidate.base_commit != self.base_commit:
            reasons.append(f"base moved from {self.base_commit} to {candidate.base_commit}")
        if candidate.repository != self.repository:
            reasons.append(f"repository is {candidate.repository}, report is for {self.repository}")
        return reasons


# --- Reading test files ---------------------------------------------------------


class ParseError(ValueError):
    pass


_KEYWORDS_BEFORE_EXPR = frozenset(
    "return typeof case do else in of new delete void throw yield await instanceof".split()
)
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "0": "\0", "b": "\b", "f": "\f", "v": "\v"}
_ESCAPE_RE = re.compile(r"\\(u\{[0-9A-Fa-f]{1,6}\}|u[0-9A-Fa-f]{4}|x[0-9A-Fa-f]{2}|\r\n|.)", re.S)


def _unescape(raw: str) -> str:
    def one(m: re.Match[str]) -> str:
        e = m.group(1)
        if e[0] in "ux" and len(e) > 1:
            code = int(e[1:].strip("{}"), 16)
            return chr(code) if code <= 0x10FFFF else ""
        if e in ("\n", "\r\n", "\r", "\u2028", "\u2029"):
            return ""  # line continuation
        return _ESCAPES.get(e, e)

    return _ESCAPE_RE.sub(one, raw)


class _Scanner:
    """Marks which characters of a JS/TS file are code (not comments, strings,
    template text or regex literals) and records every string literal."""

    def __init__(self, text: str) -> None:
        self.t = text
        self.code = bytearray(len(text))
        self.strings: dict[int, tuple[int, str | None]] = {}
        """quote position -> (closing quote position, value or None if interpolated)."""
        self.comments: list[tuple[int, int]] = []
        self._code(0, in_interp=False)

    def _code(self, i: int, *, in_interp: bool) -> int:
        t, n = self.t, len(self.t)
        depth = 0
        operand = False  # was the last token something a '/' would divide?
        while i < n:
            c = t[i]
            if c in " \t\r\n":
                i += 1
                continue
            if t.startswith("//", i):
                j = t.find("\n", i)
                self.comments.append((i, n if j < 0 else j))
                i = n if j < 0 else j
                continue
            if t.startswith("/*", i):
                j = t.find("*/", i + 2)
                if j < 0:
                    raise ParseError("unterminated comment")
                self.comments.append((i, j + 2))
                i = j + 2
                continue
            if c in "'\"":
                i, operand = self._quoted(i), True
                continue
            if c == "`":
                i, operand = self._template(i), True
                continue
            if c == "/" and not operand:
                i, operand = self._regex(i), True
                continue
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (t[j].isalnum() or t[j] in "_$"):
                    j += 1
                self.code[i:j] = b"\x01" * (j - i)
                operand = t[i:j] not in _KEYWORDS_BEFORE_EXPR
                i = j
                continue
            if t.startswith("++", i) or t.startswith("--", i):
                # Postfix keeps an operand before a '/', prefix keeps none.
                self.code[i : i + 2] = b"\x01\x01"
                i += 2
                continue
            if in_interp:
                if c == "{":
                    depth += 1
                elif c == "}":
                    if depth == 0:
                        return i
                    depth -= 1
            self.code[i] = 1
            operand = c in ")]"
            i += 1
        if in_interp:
            raise ParseError("unterminated ${ in a template literal")
        return n

    def _quoted(self, i: int) -> int:
        t, q, j = self.t, self.t[i], i + 1
        while j < len(t) and t[j] != q:
            if t[j] == "\n":
                raise ParseError("unterminated string")
            j += 2 if t[j] == "\\" else 1
        if j >= len(t):
            raise ParseError("unterminated string")
        self.code[i] = self.code[j] = 1
        self.strings[i] = (j, _unescape(t[i + 1 : j]))
        return j + 1

    def _template(self, i: int) -> int:
        t, j, interpolated = self.t, i + 1, False
        while j < len(t) and t[j] != "`":
            if t[j] == "\\":
                j += 2
            elif t.startswith("${", j):
                interpolated = True
                j = self._code(j + 2, in_interp=True) + 1
            else:
                j += 1
        if j >= len(t):
            raise ParseError("unterminated template literal")
        self.code[i] = self.code[j] = 1
        self.strings[i] = (j, None if interpolated else _unescape(t[i + 1 : j]))
        return j + 1

    def _regex(self, i: int) -> int:
        t, j, in_class = self.t, i + 1, False
        while j < len(t):
            c = t[j]
            if c == "\n":
                raise ParseError("unterminated regular expression")
            if c == "\\":
                j += 2
                continue
            if in_class:
                in_class = c != "]"
            elif c == "[":
                in_class = True
            elif c == "/":
                break
            j += 1
        if j >= len(t):
            raise ParseError("unterminated regular expression")
        j += 1
        while j < len(t) and t[j].isalpha():
            j += 1
        return j

    def masked(self) -> str:
        return "".join(c if self.code[i] else " " for i, c in enumerate(self.t))


_OPEN, _CLOSE = "([{", ")]}"
_WS = " \t\r\n"


def _match(masked: str, i: int) -> int:
    """Index of the bracket closing the one at ``i``."""
    stack = []
    for j in range(i, len(masked)):
        c = masked[j]
        if c in _OPEN:
            stack.append(_CLOSE[_OPEN.index(c)])
        elif c in _CLOSE:
            if not stack or stack.pop() != c:
                raise ParseError(f"unbalanced {c!r}")
            if not stack:
                return j
    raise ParseError("unclosed bracket")


def _match_back(masked: str, j: int, floor: int) -> int:
    """Index of the bracket opening the one closing at ``j``, or -1."""
    depth = 0
    for k in range(j, floor - 1, -1):
        c = masked[k]
        if c in _CLOSE:
            depth += 1
        elif c in _OPEN:
            depth -= 1
            if depth == 0:
                return k
    return -1


def _skip_ws(s: str, i: int) -> int:
    while i < len(s) and s[i] in _WS:
        i += 1
    return i


def _word_before(m: str, j: int, floor: int) -> tuple[str, int]:
    """The identifier ending at ``j`` (inclusive) and where it starts."""
    k = j
    while k >= floor and (m[k].isalnum() or m[k] in "_$"):
        k -= 1
    return m[k + 1 : j + 1], k + 1


_IDENT_RE = re.compile(r"[A-Za-z_$][\w$]*")
_CALL_NAMES = "describe|suite|it|test|xit|xtest|xdescribe|fit|fdescribe"
_CALL_RE = re.compile(rf"(?<![\w$.])({_CALL_NAMES})(?![\w$])")
_SUITE_FNS = frozenset({"describe", "suite", "xdescribe", "fdescribe"})
_SKIP_MODS = frozenset({"skip", "todo", "fails"})
_COND_MODS = frozenset({"skipIf", "runIf"})
_PARAM_MODS = frozenset({"each", "for"})
_KNOWN_MODS = (
    _SKIP_MODS | _COND_MODS | _PARAM_MODS | {"only", "concurrent", "sequential", "shuffle"}
)
_SAFE_OPTIONS = frozenset({"timeout", "retry", "repeats", "concurrent", "sequential", "shuffle"})
# Names a file must not redefine: if it does, nothing it calls by them can be trusted.
_GUARDED = (
    "describe|suite|it|test|xit|xtest|xdescribe|fit|fdescribe|expect|assert|vi|"
    "beforeEach|beforeAll|afterEach|afterAll"
)
_SHADOW_RES = (
    re.compile(rf"(?<![\w$.])(?:function\s*\*?|class|const|let|var)\s+({_GUARDED})(?![\w$])"),
    re.compile(rf"(?<![\w$.])({_GUARDED})\s*(?:[-+*/%&|^]|\*\*|<<|>>>?|&&|\|\||\?\?)?=(?![=>])"),
)
_DESTRUCTURE_RE = re.compile(r"(?<![\w$.])(?:const|let|var)\s*([\[{])")
# Ways to skip, or reach a test function, that are not part of a test's declaration.
_RUNTIME_SKIP_RES = (
    re.compile(r"\.\s*skip\s*(?:\?\.\s*)?\("),
    re.compile(r"(?<![\w$.])skip\s*\("),
)
_GLOBALS = r"(?:globalThis|global|window|self)"
_GLOBAL_WRITE_RES = (
    re.compile(rf"(?<![\w$.]){_GLOBALS}\s*\.\s*(?:{_GUARDED})(?![\w$])"),
    re.compile(r"(?<![\w$.])(?:Reflect|Object)\s*\["),
    re.compile(r"(?<![\w$.])(?:eval|Function)\s*\("),
)
# Object.assign(globalThis, ...), Reflect.set(window, ...): checked for guarded names.
_GLOBAL_CALL_RE = re.compile(
    rf"(?<![\w$.])(?:Reflect|Object)\s*\.\s*[A-Za-z]+\s*\(\s*(?:\(\s*)?{_GLOBALS}(?![\w$])"
)
_GUARDED_WORD_RE = re.compile(rf"(?<![\w$.])(?:{_GUARDED})(?![\w$])(?!\s*\.)")
_INDIRECT_WORDS = frozenset(
    _GUARDED.split("|") + ["skip", "only", "todo", "fails", "skipIf", "runIf", "skipped"]
)


@dataclass(frozen=True)
class _Block:
    fn: str
    """The function the test is declared with: describe, it, test..."""
    is_suite: bool
    title: str | None
    mods: frozenset[str]
    start: int
    head_end: int
    """Position of the call's opening parenthesis."""
    end: int
    body: tuple[int, int] | None
    line: int
    parent: int | None
    """Index of the innermost enclosing suite block."""


@dataclass(frozen=True)
class _Test:
    name: str | None
    block: _Block
    skipped: bool
    conditional: bool
    parameterized: bool
    only: bool
    unregistered: tuple[str, ...]
    """Why the test may never be registered (inside a function, condition...)."""


@dataclass(frozen=True)
class _File:
    text: str
    masked: str
    code: bytes
    blocks: tuple[_Block, ...]
    tests: tuple[_Test, ...]
    has_only: bool
    problems: tuple[str, ...]
    """File-level reasons no test in it can be trusted (runtime skips, globals)."""
    shadowed: tuple[tuple[str, str], ...]
    """(name, why) for each test function or assertion name the file redefines."""
    imports: tuple[tuple[str, str], ...]
    """(local name, module specifier) for every value import."""
    namespaces: tuple[str, ...]
    """Local names of ``import * as name`` imports."""
    import_spans: tuple[tuple[int, int], ...]
    """(start, end) of every ``import ... from '...'`` statement."""
    comments: tuple[tuple[int, int], ...]
    mocks: tuple[str, ...]
    """Module specifiers mocked with vi.mock/vi.doMock; "?" if not a plain string."""
    spies: tuple[str, ...]
    """First-argument text of every vi.spyOn call."""
    newlines: tuple[int, ...]

    def line(self, pos: int) -> int:
        return bisect.bisect_right(self.newlines, pos - 1) + 1


def _parse(text: str) -> _File:
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    scan = _Scanner(text)
    masked = scan.masked()
    newlines = tuple(i for i, c in enumerate(text) if c == "\n")
    imports, namespaces, import_spans = _imports(text, scan)
    problems: list[str] = []

    def line(pos: int) -> int:
        return bisect.bisect_right(newlines, pos - 1) + 1

    def in_import(pos: int) -> bool:
        return any(a <= pos < b for a, b in import_spans)

    shadowed = _shadowing(masked, imports)
    declared = {name for name, _ in shadowed}
    blocks: list[_Block] = []
    suites: list[int] = []  # stack of open suite block indices
    for m in _CALL_RE.finditer(masked):
        if in_import(m.start()) or _is_key(masked, m.start(), m.end()):
            continue
        fn = m.group(1)
        mods = set()
        if fn[0] == "x":
            mods.add("skip")
        elif fn[0] == "f":
            mods.add("only")
        j, odd = m.end(), False
        while True:
            k = _skip_ws(masked, j)
            if k >= len(masked) or masked[k] != ".":
                break
            mm = _IDENT_RE.match(masked, _skip_ws(masked, k + 1))
            if not mm or mm.group(0) not in _KNOWN_MODS:
                odd = True
                break
            mods.add(mm.group(0))
            j = _skip_ws(masked, mm.end())
            if mm.group(0) in _COND_MODS | _PARAM_MODS and j < len(masked):
                if masked[j] == "(":
                    j = _match(masked, j) + 1
                elif j in scan.strings:
                    j = scan.strings[j][0] + 1
        j = _skip_ws(masked, j)
        if odd or j >= len(masked) or masked[j] != "(":
            if fn in declared:
                continue  # the file's own variable; calling it is caught per test
            problems.append(
                f"line {line(m.start())}: uses {fn!r} other than as a direct call, so which "
                "tests run cannot be read from the source"
            )
            continue
        close = _match(masked, j)
        k = _skip_ws(masked, j + 1)
        title = None
        if k in scan.strings:
            end, title = scan.strings[k]
            k = end + 1
        body, arg_problems = _call_args(masked, k, close)
        problems += [f"line {line(m.start())}: {x}" for x in arg_problems]
        while suites and not (
            blocks[suites[-1]].body
            and blocks[suites[-1]].body[0] <= m.start() < blocks[suites[-1]].body[1]
        ):
            suites.pop()
        blocks.append(
            _Block(
                fn=fn,
                is_suite=fn in _SUITE_FNS,
                title=title,
                mods=frozenset(mods),
                start=m.start(),
                head_end=j,
                end=close,
                body=body,
                line=line(m.start()),
                parent=suites[-1] if suites else None,
            )
        )
        if fn in _SUITE_FNS and body:
            suites.append(len(blocks) - 1)

    probe = _File(
        text=text,
        masked=masked,
        code=bytes(scan.code),
        blocks=tuple(blocks),
        tests=(),
        has_only=False,
        problems=(),
        shadowed=(),
        imports=(),
        namespaces=(),
        import_spans=(),
        comments=(),
        mocks=(),
        spies=(),
        newlines=newlines,
    )
    unregistered = _registration(probe)

    tests = []
    for idx, b in enumerate(blocks):
        if b.is_suite:
            continue
        chain = []
        p = b.parent
        while p is not None:
            chain.append(p)
            p = blocks[p].parent
        chain.reverse()
        names = [blocks[s].title for s in chain] + [b.title]
        mods = set().union(*(blocks[s].mods for s in chain), b.mods)
        why = [r for s in [*chain, idx] for r in unregistered.get(s, ())]
        tests.append(
            _Test(
                name=None if any(n is None for n in names) else _SEP.join(names),
                block=b,
                skipped=bool(mods & _SKIP_MODS) or b.body is None,
                conditional=bool(mods & _COND_MODS),
                parameterized=bool(mods & _PARAM_MODS),
                only="only" in mods,
                unregistered=tuple(why),
            )
        )

    heads = [(b.start, b.head_end) for b in blocks]
    for rx in _RUNTIME_SKIP_RES:
        hit = next(
            (x for x in rx.finditer(masked) if not any(a <= x.start() < e for a, e in heads)), None
        )
        if hit:
            problems.append(f"line {line(hit.start())}: skips tests at runtime")
    indirect = "reaches globals or code indirectly, which can replace the test functions"
    for rx in _GLOBAL_WRITE_RES:
        hit = next(rx.finditer(masked), None)
        if hit:
            problems.append(f"line {line(hit.start())}: {indirect}")
    for hit in _GLOBAL_CALL_RE.finditer(masked):
        try:
            args = text[hit.start() : _match(masked, masked.index("(", hit.start())) + 1]
        except ParseError:
            args = "["
        if _GUARDED_WORD_RE.search(args) or "[" in args or "..." in args:
            problems.append(f"line {line(hit.start())}: {indirect}")
            break
    for q, (end, value) in scan.strings.items():
        # A guarded word as a computed key: x['skip'](), globalThis['it'] = ...
        if (
            value in _INDIRECT_WORDS
            and _before(masked, q) == "["
            and masked[_skip_ws(masked, end + 1) : _skip_ws(masked, end + 1) + 1] == "]"
        ):
            problems.append(
                f"line {line(q)}: uses {value!r} as a computed key, which can reach test "
                "functions or skip tests indirectly"
            )
            break
    for close in re.finditer(r"\]\s*(?:\?\.\s*)?\(", masked):
        k = _match_back(masked, close.start(), 0)
        if k > 0 and (masked[k - 1].isalnum() or masked[k - 1] in "_$)]"):
            key = masked[k + 1 : close.start()].strip()
            if not re.fullmatch(r"\d+", key):
                problems.append(
                    f"line {line(k)}: calls a computed member, which can skip tests or reach "
                    "test functions indirectly"
                )
                break

    return _File(
        text=text,
        masked=masked,
        code=bytes(scan.code),
        blocks=tuple(blocks),
        tests=tuple(tests),
        has_only=any("only" in b.mods for b in blocks),
        problems=tuple(dict.fromkeys(problems)),
        shadowed=tuple(shadowed),
        imports=tuple(imports),
        namespaces=tuple(namespaces),
        import_spans=tuple(import_spans),
        comments=tuple(scan.comments),
        mocks=tuple(_mocks(masked, scan)),
        spies=tuple(_spies(masked)),
        newlines=newlines,
    )


def _call_args(masked: str, k: int, close: int):
    """(callback body range or None, problems) for a test call's arguments after
    the title. Only an options object with plain known keys and literal values,
    a function literal and a number (a timeout) can be followed; anything else
    (a variable, a quoted or computed key) could carry skip or only."""
    args, depth, start = [], 0, k
    for i in range(k, close):
        c = masked[i]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            args.append((start, i))
            start = i + 1
    args.append((start, close))
    body, problems = None, []
    for a, e in args:
        a = _skip_ws(masked, a)
        text = masked[a:e].strip()
        if not text:
            continue
        if text[0] == "{" and _match(masked, a) == a + len(text) - 1:
            inner = text[1:-1]
            keys = re.findall(r"(?:^|,)\s*([^,:]*?)\s*:", inner)
            plain = all(_IDENT_RE.fullmatch(x) for x in keys) and "..." not in inner
            values = set(_idents(inner)) - set(keys)
            if (
                not plain
                or set(keys) - _SAFE_OPTIONS
                or values - _LITERAL_WORDS
                or "'" in inner
                or '"' in inner
                or "`" in inner
                or "[" in inner
            ):
                problems.append(
                    "an options argument that is not plain known keys with literal values can "
                    "skip tests or mark them only or failing"
                )
            continue
        if re.fullmatch(r"\d[\d_]*", text):
            continue
        found = _function_body(masked, a, e)
        if found is not None and body is None:
            body = found
            continue
        problems.append(
            f"passes an argument ({_normalize(text)[:40]}) the reader cannot follow; it could "
            "carry skip or only"
        )
    return body, problems


def _function_body(masked: str, a: int, e: int) -> tuple[int, int] | None:
    """The body of the function literal spanning [a, e), or None if it is not one."""
    head = re.match(
        r"(?:async\s+)?(?:function\b[^(]*|[A-Za-z_$][\w$]*\s*(?==>)|(?=\())", masked[a:e]
    )
    if head is None:
        return None
    i = a + head.end()
    if masked[i] == "(":
        i = _skip_ws(masked, _match(masked, i) + 1)
        if masked[i] == ":":  # a return type annotation
            while i < e and not masked.startswith("=>", i) and masked[i] != "{":
                i += 1
    else:
        i = _skip_ws(masked, i)
    if masked.startswith("=>", i):
        q = _skip_ws(masked, i + 2)
        if masked[q] == "{":
            end = _match(masked, q)
            return (q + 1, end) if _skip_ws(masked, end + 1) >= e else None
        return (q, e)
    if masked[i] == "{" and "function" in masked[a:i]:
        end = _match(masked, i)
        return (i + 1, end) if _skip_ws(masked, end + 1) >= e else None
    return None


def _before(masked: str, i: int) -> str:
    """The last non-space character before ``i``, or ""."""
    b = i - 1
    while b >= 0 and masked[b] in _WS:
        b -= 1
    return masked[b] if b >= 0 else ""


def _is_key(masked: str, start: int, end: int) -> bool:
    """Whether the name at [start, end) is an object key or a type member."""
    a = _skip_ws(masked, end)
    if a >= len(masked) or masked[a] not in ":?":
        return False
    b = start - 1
    while b >= 0 and masked[b] in _WS:
        b -= 1
    return b >= 0 and masked[b] in "{,;"


_FUNCTION_RE = re.compile(r"function\b")


def _registration(f: _File) -> dict[int, list[str]]:
    """For each block, why it may never be registered: it sits inside a
    function, condition or block, behind a condition, or after a return."""
    out: dict[int, list[str]] = {}
    children: dict[int | None, list[int]] = {}
    for idx, b in enumerate(f.blocks):
        children.setdefault(b.parent, []).append(idx)
    for parent, kids in children.items():
        floor = f.blocks[parent].body[0] if parent is not None else 0
        states = _walk(f, floor, [f.blocks[k].start for k in kids])
        for k, (stack, exits) in zip(kids, states, strict=True):
            why = []
            if stack:
                why.append("is declared inside a function, condition or block")
            if exits:
                why.append(f"comes after an early exit on line {exits[0][0]}")
            guard = _guard(f, floor, f.blocks[k].start)
            if guard:
                why.append(guard)
            if why:
                out[k] = [f"line {f.blocks[k].line}: " + "; ".join(why)]
    return out


def _walk(f: _File, start: int, queries: list[int]):
    """Walk the code from ``start`` and report, at each query position (sorted),
    what encloses it and which return statements before it can end the
    enclosing body early (a throw fails the test instead).

    Each state is (stack, exits): stack holds "fn", "block" or "paren" for each
    open bracket (plus "fn" for an expression-bodied arrow), exits holds
    (line, conditional) for each return not inside a nested function.
    A ``return`` immediately followed by the query position returns it and is
    not an early exit.
    """
    m = f.masked
    stack: list[str] = []
    exits: list[tuple[int, bool, int]] = []
    pending: int | None = None  # stack depth where a function head was seen
    states = []
    qi = 0
    i = start
    end = max(queries, default=start)
    while i <= end and qi < len(queries):
        while qi < len(queries) and i >= queries[qi]:
            q = queries[qi]
            kinds = list(stack) + (["fn"] if pending == len(stack) else [])
            # A `return` right before the query returns the queried statement itself.
            here = [(ln, cond) for ln, cond, after in exits if after != q]
            states.append((kinds, here))
            qi += 1
        if qi >= len(queries) or i >= len(m):
            break
        c = m[i]
        if c.isalpha() or c in "_$":
            j = i
            while j < len(m) and (m[j].isalnum() or m[j] in "_$"):
                j += 1
            word = m[i:j]
            if not (i and m[i - 1] == "."):
                if word == "function":
                    pending = len(stack)
                elif word == "return" and "fn" not in stack:
                    # A throw fails the test; only a return can end it early and green.
                    cond = bool(stack) or _guard(f, start, i) is not None
                    exits.append((f.line(i), cond, _skip_ws(m, j)))
            i = j
            continue
        if m.startswith("=>", i):
            pending = len(stack)
            i += 2
            continue
        if c in _OPEN:
            if c == "{" and pending == len(stack):
                stack.append("fn")
                pending = None
            else:
                stack.append("block" if c == "{" else "paren")
        elif c in _CLOSE:
            if stack:
                stack.pop()
            if pending is not None and pending > len(stack):
                pending = None
        elif c == ";" and pending == len(stack):
            pending = None
        i += 1
    while len(states) < len(queries):
        states.append((list(stack), [(ln, cond) for ln, cond, _ in exits]))
    return states


_UNARY_WORDS = frozenset({"await", "return", "void", "typeof", "delete", "new", "yield"})
_EXPR_WORDS = frozenset({"in", "instanceof", "of", "case", "extends", "throw"})


def _guard(f: _File, floor: int, pos: int) -> str | None:
    """Why the statement at ``pos`` may not run even when the code before it
    does: it is not at the start of a statement but behind a condition, a
    loop header or an operator."""
    m = f.masked
    j = pos - 1
    while True:
        while j >= floor and m[j] in _WS:
            j -= 1
        if j < floor:
            return None
        c = m[j]
        if c in ";{}":
            return None
        if c == ")":
            k = _match_back(m, j, floor)
            if k < 0:
                return "follows an unmatched parenthesis"
            w = k - 1
            while w >= floor and m[w] in _WS:
                w -= 1
            word, _ = _word_before(m, w, floor)
            if word in ("if", "for", "while", "with"):
                return f"runs only when its `{word}` condition holds"
            return None  # the end of the previous statement, without a semicolon
        if c.isalnum() or c in "_$":
            word, s = _word_before(m, j, floor)
            if word in _UNARY_WORDS:
                j = s - 1  # step back over the keyword and look again
                continue
            if word in ("else", "do"):
                return f"is in an `{word}` branch"
            if word in _EXPR_WORDS:
                return f"is part of an expression after `{word}`, so it may not run"
            return None  # the end of the previous statement, without a semicolon
        if c == "]":
            return None
        return f"is part of an expression after `{c}`, so it may not run"


def _shadowing(masked, imports) -> list[tuple[str, str]]:
    out = []
    for local, spec in imports:
        if re.fullmatch(_GUARDED, local) and spec != "vitest":
            out.append((local, f"imports {local!r} from {spec!r} instead of vitest"))
    for rx in _SHADOW_RES:
        for m in rx.finditer(masked):
            out.append((m.group(1), f"redefines {m.group(1)!r}"))
    for m in _DESTRUCTURE_RE.finditer(masked):
        try:
            inner = masked[m.start(1) + 1 : _match(masked, m.start(1))]
        except ParseError:
            continue
        for name in _idents(inner):
            if re.fullmatch(_GUARDED, name):
                out.append((name, f"redefines {name!r}"))
    for m in re.finditer(rf"\.\s*({_GUARDED})\s*=(?![=>])", masked):
        out.append((m.group(1), f"assigns {m.group(1)!r} on an object"))
    return list(dict.fromkeys(out))


_IMPORT_RE = re.compile(
    r"\bimport\s+(?P<type>type\s+)?(?P<clause>[\w$*{}\s,]+?)\s+from\s+(?=[\"'])", re.S
)


def _imports(text: str, scan: _Scanner):
    out, namespaces, spans = [], [], []
    for m in _IMPORT_RE.finditer(text):
        if not scan.code[m.start()] or m.end() not in scan.strings:
            continue
        spans.append((m.start(), scan.strings[m.end()][0] + 1))
        if m.group("type"):
            continue
        spec = scan.strings[m.end()][1] or ""
        for part in re.split(r"[{},]", m.group("clause")):
            part = part.strip()
            if not part or part.startswith("type "):
                continue
            local = part.split(" as ")[-1].strip()
            if _IDENT_RE.fullmatch(local):
                out.append((local, spec))
                if part.startswith("*"):
                    namespaces.append(local)
    return out, namespaces, spans


_MOCK_RE = re.compile(r"(?<![\w$.])vi\s*\.\s*(?:mock|doMock)\s*\(")
_SPY_RE = re.compile(r"(?<![\w$.])vi\s*\.\s*(?:spyOn|stubGlobal|stubEnv)\s*\(")


def _mocks(masked: str, scan: _Scanner):
    for m in _MOCK_RE.finditer(masked):
        k = _skip_ws(masked, m.end())
        yield (scan.strings[k][1] or "?") if k in scan.strings else "?"


def _spies(masked: str):
    for m in _SPY_RE.finditer(masked):
        try:
            close = _match(masked, m.end() - 1)
        except ParseError:
            yield "?"
            continue
        yield masked[m.end() : close].split(",")[0].strip() or "?"


def _is_product(test_path: str, spec: str) -> bool:
    """A relative or root-relative import that resolves outside ``tests/``."""
    if spec == "?":
        return True  # not a plain string: assume the worst
    if spec.startswith("/"):
        resolved = posixpath.normpath(spec.lstrip("/"))
    elif spec.startswith("."):
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(test_path), spec))
    else:
        return False
    return not resolved.startswith(("tests/", "../")) and resolved not in ("tests", "..", ".")


def _idents(masked: str) -> list[str]:
    """Identifiers used as values: not property names after a dot, and not
    object keys (``{ key: value }``)."""
    out = []
    for m in _IDENT_RE.finditer(masked):
        s = m.start()
        if s and (masked[s - 1].isalnum() or masked[s - 1] in "_$"):
            continue  # inside a number such as 1e5
        b = s - 1
        while b >= 0 and masked[b] in _WS:
            b -= 1
        if b >= 0 and masked[b] == "." and masked[max(0, b - 2) : b + 1] != "...":
            continue  # a property name, not a spread
        a = _skip_ws(masked, m.end())
        if a < len(masked) and masked[a] == ":" and b >= 0 and masked[b] in "{,":
            continue
        out.append(m.group(0))
    return out


_ASSERT_RE = re.compile(
    r"(?<![\w$.])(expect(?:\s*\.\s*soft)?|assert(?:\s*\.\s*\w+)?)\s*(?:<[^()]*>\s*)?\("
)
_MATCHER_RE = re.compile(
    r"(?P<mods>(?:\s*\.\s*(?:not|resolves|rejects))*)\s*\.\s*(?P<name>\w+)\s*\("
)
_LITERAL_WORDS = frozenset({"true", "false", "null", "undefined", "NaN", "Infinity"})
_WEAK_MATCHERS = frozenset(
    {
        "toBeDefined",
        "toBeTruthy",
        "toBeFalsy",
        "toBeInstanceOf",
        "toSatisfy",
        "toBeTypeOf",
        "toHaveBeenCalled",
        "toMatchSnapshot",
        "toMatchInlineSnapshot",
        "toMatchFileSnapshot",
    }
)
_BOUND_MATCHERS = frozenset(
    {"toBeGreaterThan", "toBeGreaterThanOrEqual", "toBeLessThan", "toBeLessThanOrEqual"}
)
_WEAK_ASSERTS = frozenset({"assert", "assert.ok", "assert.isOk", "assert.exists"})


@dataclass(frozen=True)
class _Assertion:
    start: int
    end: int
    call: str
    """``expect``, ``expect.soft``, ``assert`` or ``assert.<name>``."""
    subject: str
    """Masked text of the asserted value."""
    expected: str | None
    """Masked text of the matcher's arguments (None for assert-style)."""
    matcher: str | None
    negated: bool


def _assertion_at(f: _File, i: int, limit: int) -> _Assertion | None:
    """The first complete assertion starting at or after ``i`` and before ``limit``."""
    for m in _ASSERT_RE.finditer(f.masked, i, limit):
        open_ = m.end() - 1
        close = _match(f.masked, open_)
        subject = f.masked[open_ + 1 : close]
        call = re.sub(r"\s", "", m.group(1))
        if call.startswith("assert"):
            args = _split_args(subject)
            comparing = re.fullmatch(r"assert\.(?!not)\w*(?:[Ee]qual|[Ss]ame)\w*", call)
            if comparing and len(args) >= 2:
                return _Assertion(m.start(), close + 1, call, args[0], args[1], call, False)
            return _Assertion(m.start(), close + 1, call, subject, None, None, False)
        mm = _MATCHER_RE.match(f.masked, close + 1)
        if not mm:
            continue  # expect(x) with no matcher asserts nothing
        mclose = _match(f.masked, mm.end() - 1)
        return _Assertion(
            m.start(),
            mclose + 1,
            call,
            subject,
            f.masked[mm.end() : mclose],
            mm.group("name"),
            "not" in mm.group("mods"),
        )
    return None


def _split_args(masked: str) -> list[str]:
    out, depth, start = [], 0, 0
    for i, c in enumerate(masked):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif c == "," and depth == 0:
            out.append(masked[start:i])
            start = i + 1
    out.append(masked[start:])
    return [x for x in out if x.strip()]


def _top_level_operator(masked: str) -> str | None:
    masked = re.sub(r",\s*$", "", masked)  # prettier's trailing comma
    depth = 0
    for i, c in enumerate(masked):
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif depth == 0:
            for op in ("&&", "||", "??"):
                if masked.startswith(op, i):
                    return op
            if c == "?" and masked[i + 1 : i + 2] not in (".", "?") and masked[i - 1 : i] != "?":
                return "?"
            if c == ",":
                return ","
    return None


def _is_literal(masked_subject: str) -> bool:
    return not (set(_idents(_strip_numbers(masked_subject))) - _LITERAL_WORDS)


def _strip_numbers(s: str) -> str:
    return re.sub(r"(?<![\w$])\d[\w.]*", " ", s)


def _normalize(s: str) -> str:
    return " ".join(s.split())


def _locate(f: _File, start: int, end: int, quote: str) -> list[tuple[int, int]]:
    """Spans in [start, end) that read as ``quote`` ignoring whitespace and start in code."""
    want = _normalize(quote)
    if not want:
        return []
    chars, where = [], []
    i = start
    while i < end:
        if f.text[i].isspace():
            j = i
            while j < end and f.text[j].isspace():
                j += 1
            if chars and chars[-1] != " ":
                chars.append(" ")
                where.append(i)
            i = j
            continue
        chars.append(f.text[i])
        where.append(i)
        i += 1
    hay = "".join(chars)
    found, k = [], hay.find(want)
    while k >= 0:
        if f.code[where[k]]:
            found.append((where[k], where[k + len(want) - 1] + 1))
        k = hay.find(want, k + 1)
    return found


_DECL_RE = re.compile(r"(?<![\w$.])(?:const|let|var)\s+(?=[\[{A-Za-z_$])")
_ASSIGN_RE = re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=(?![=>])")


def _bindings(f: _File, t: _Test) -> dict[str, list[str]]:
    """Every name assigned where test ``t`` can see it (its own body, its suites
    and the top level, but not inside other tests or suites), with the masked
    text of what it is set to."""
    m = f.masked
    out: dict[str, list[str]] = {}
    own = {f.blocks.index(t.block)}
    p = t.block.parent
    while p is not None:
        own.add(p)
        p = f.blocks[p].parent
    merged: list[list[int]] = []
    for a, e in sorted(
        (b.start, b.end) for i, b in enumerate(f.blocks) if i not in own and b.body is not None
    ):
        if merged and a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([a, e])
    starts = [a for a, _ in merged]

    def visible(pos: int) -> bool:
        k = bisect.bisect_right(starts, pos) - 1
        return k < 0 or pos > merged[k][1]

    def expr_from(i: int) -> str:
        depth, j = 0, i
        while j < len(m):
            c = m[j]
            if c in _OPEN:
                depth += 1
            elif c in _CLOSE:
                if depth == 0:
                    break
                depth -= 1
            elif depth == 0 and c in ";,":
                break
            j += 1
        return m[i:j]

    for d in _DECL_RE.finditer(m):
        if not visible(d.start()):
            continue
        k = d.end()
        if m[k] in "[{":
            try:
                close = _match(m, k)
            except ParseError:
                continue
            names, k = _idents(m[k + 1 : close]), close + 1
        else:
            ident = _IDENT_RE.match(m, k)
            names, k = [ident.group(0)], ident.end()
        k = _skip_ws(m, k)
        if k < len(m) and m[k] == ":":  # a type annotation
            while k < len(m) and m[k] not in "=;\n":
                k += 1
        if k < len(m) and m[k] == "=" and m[k + 1 : k + 2] not in ("=", ">"):
            value = expr_from(k + 1)
            for name in names:
                out.setdefault(name, []).append(value)
    for a in _ASSIGN_RE.finditer(m):
        if not visible(a.start()):
            continue
        out.setdefault(a.group(1), []).append(expr_from(a.end()))
    for fn in _FUNCTION_DECL_RE.finditer(m):
        if not visible(fn.start()):
            continue
        p = m.find("(", fn.end())
        if p < 0:
            continue
        try:
            q = _match(m, p)
            b = m.find("{", q)
            out.setdefault(fn.group(1), []).append(m[b : _match(m, b) + 1] if b >= 0 else "")
        except ParseError:
            continue
    return out


_FUNCTION_DECL_RE = re.compile(r"(?<![\w$.])function\s*\*?\s*([A-Za-z_$][\w$]*)")
_TYPEOF_RE = re.compile(r"(?<![\w$.])typeof\s+[\w$.]+")


def _traced(expr: str, product: set[str], bindings, depth: int = 4, seen=frozenset()) -> bool:
    """Whether a value comes from product code: it uses a product name directly
    (not just its ``typeof``), or uses a name every assignment of which does."""
    expr = _TYPEOF_RE.sub(" ", expr)
    names = _idents(expr)
    if any(n in product for n in names):
        return True
    if depth == 0:
        return False
    for n in names:
        values = bindings.get(n)
        if n in seen or not values:
            continue
        if all(_traced(v, product, bindings, depth - 1, seen | {n}) for v in values):
            return True
    return False


_CALL_SITE_RE = re.compile(
    r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*(?:\.\s*([A-Za-z_$][\w$]*)\s*)?(?:<[^()]*>\s*)?\("
)


def _calls(expr: str, product: set[str], namespaces: set[str], aliases) -> set[str]:
    """The product functions an expression calls, by their exported names."""
    out = set()
    for m in _CALL_SITE_RE.finditer(expr):
        a, b = m.group(1), m.group(2)
        if a in namespaces and b:
            out.add(b)
        elif not b and a in aliases:
            out.add(aliases[a])
        elif not b and a in product:
            out.add(a)
    return out


def _aliases(product: set[str], namespaces: set[str], bindings) -> dict[str, str]:
    out = {}
    for name, values in bindings.items():
        for v in values:
            v = _normalize(v)
            ns = re.fullmatch(r"([A-Za-z_$][\w$]*)\s*\.\s*([A-Za-z_$][\w$]*)", v)
            if v in product and v not in namespaces:
                out[name] = v
            elif ns and ns.group(1) in namespaces:
                out[name] = ns.group(2)
    return out


def _assertion_count(f: _File, t: _Test) -> int:
    if t.block.body is None:
        return 0
    s, e = t.block.body
    nested = [(b.start, b.end) for b in f.blocks if b is not t.block and s <= b.start < e]
    return sum(
        1
        for m in _ASSERT_RE.finditer(f.masked, s, e)
        if not any(a <= m.start() <= b for a, b in nested)
    )


# --- The mapper -----------------------------------------------------------------


def map_assertions(
    contract: Mapping[str, object],
    approved_digest: ContractDigest | str,
    candidate: Candidate,
    verification: Report,
    links: Iterable[AssertionLink] = (),
    sources: Iterable[TestSource] = (),
    proofs: Iterable[FailureProof] = (),
    limits: Iterable[FailureProofLimit] = (),
    control_change: ControlChangeReport | None = None,
    clearances: Iterable[Clearance] = (),
    policy: AssertionPolicy = DEFAULT_ASSERTION_POLICY,
) -> AssertionReport:
    """Map every acceptance criterion of ``contract`` to what shows it on ``candidate``.

    ``approved_digest`` must come from Rolando's approval record, never from
    the PR. ``verification`` is ENG-156's report for the same candidate. Inputs
    are read, never changed.
    """
    links, proofs, limits = tuple(links), tuple(proofs), tuple(limits)
    clearances = tuple(clearances)
    want = str(approved_digest)
    ignored: list[str] = []

    def report(gates, criteria=(), flags=()):
        return AssertionReport(
            contract_digest=want,
            repository=candidate.repository,
            candidate_commit=candidate.head_commit,
            base_commit=candidate.base_commit,
            gates=tuple(gates),
            criteria=tuple(criteria),
            flags=tuple(flags),
            verification=verification,
            ignored=tuple(ignored),
        )

    errors = validate(contract, approved_digest)
    gates = [
        Gate(
            "contract",
            not errors,
            "matches the approved digest" if not errors else "; ".join(errors),
        )
    ]
    if errors:
        return report(gates)
    stale = verification.still_current(candidate)
    if verification.contract_digest != want:
        stale.append(f"it is for contract {verification.contract_digest}, not {want}")
    gates.append(
        Gate(
            "criterion check",
            not stale,
            "is for this contract and revision" if not stale else "; ".join(stale),
        )
    )
    if stale:
        return report(gates)

    def for_contract(items, what):
        keep = []
        for x in items:
            if x.contract_digest == want:
                keep.append(x)
            else:
                ignored.append(
                    f"stale: {what} for {getattr(x, 'criterion', None) or getattr(x, 'flag', '?')} "
                    f"is for contract {x.contract_digest[:12]}, not the approved {want[:12]}"
                )
        return tuple(keep)

    links = for_contract(links, "link")
    proofs = for_contract(proofs, "failure proof")
    limits = for_contract(limits, "failure-proof limit")
    clearances = for_contract(clearances, "clearance")

    head, base = _index_sources(sources, candidate, ignored)
    parsed: dict[tuple[str, str], _File | str] = {}

    def parse(src: TestSource) -> _File | str:
        key = (src.path, src.commit)
        if key not in parsed:
            try:
                parsed[key] = _parse(src.text or "")
            except (ParseError, IndexError, RecursionError) as e:
                parsed[key] = (
                    f"{src.path} at {src.commit[:12]} could not be read reliably: "
                    f"{type(e).__name__} {e}"
                )
        return parsed[key]

    flags: dict[str, Flag] = {}

    def flag(f: Flag) -> None:
        old = flags.get(f.key)
        if old is None:
            flags[f.key] = f
        elif f.detail not in old.detail.split("; "):
            flags[f.key] = replace(old, detail=f"{old.detail}; {f.detail}")

    for f in _control_flags(candidate, control_change, policy, ignored):
        flag(f)
    for f in _test_change_flags(candidate, head, base, parse):
        flag(f)
    for f in _suppression_flags(candidate, head, base):
        flag(f)

    by_id = {v.criterion: v for v in verification.criteria}
    coverage = []
    for c in contract["acceptance_criteria"]:
        cov, link_flags = _criterion(
            c, by_id.get(c["id"]), candidate, links, head, parse, proofs, limits, policy, ignored
        )
        coverage.append(cov)
        for f in link_flags:
            flag(f)

    known = {c["id"] for c in contract["acceptance_criteria"]}
    for link in links:
        if link.criterion not in known:
            ignored.append(f"link by {link.mapper} names unknown criterion {link.criterion!r}")

    final = _apply_clearances(flags, clearances, candidate, coverage, policy, ignored)
    return report(gates, coverage, final)


def _index_sources(sources, candidate, ignored):
    head: dict[str, TestSource] = {}
    base: dict[str, TestSource] = {}
    conflicts: set[tuple[str, str]] = set()
    for s in sources:
        if s.commit == candidate.head_commit:
            target = head
        elif s.commit == candidate.merge_base:
            target = base
        else:
            ignored.append(
                f"stale: source of {s.path} at {s.commit[:12]} is neither the candidate "
                "nor its merge base"
            )
            continue
        prior = target.get(s.path)
        if prior is not None and prior.text != s.text:
            conflicts.add((s.path, s.commit))
        target[s.path] = s
    for path, commit in sorted(conflicts):
        ignored.append(f"conflicting sources for {path} at {commit[:12]}: neither is used")
        (head if commit == candidate.head_commit else base).pop(path, None)
    return head, base


def _control_flags(candidate, report, policy, ignored):
    trust = policy.trust
    for path in candidate.changed_paths:
        content = any(
            path.startswith(p) if p.endswith("/") else path == p for p in trust.content_paths
        )
        name = path.rsplit("/", 1)[-1]
        if not content or any(re.search(rx, name) for rx in trust.config_names):
            yield Flag(
                FlagKind.CONTROL_CHANGE,
                path,
                f"changes {path}, which can change how the candidate's own checks run",
            )
    subject = "ci-report"
    if report is None:
        yield Flag(
            FlagKind.CONTROL_CHANGE,
            subject,
            "no control-change report from CI for this exact revision, so nobody has "
            "classified the diff",
        )
        return
    why = []
    if report.head_commit != candidate.head_commit or report.base_commit != candidate.base_commit:
        why.append(f"is for {report.head_commit[:12]} on {report.base_commit[:12]}")
    if report.repository != candidate.repository:
        why.append(f"ran in {report.repository or '?'}")
    if report.workflow_path != trust.workflow_path:
        why.append(f"came from {report.workflow_path or '?'}, not {trust.workflow_path}")
    if report.app != trust.app:
        why.append(f"was posted by {report.app or 'nobody'}, not {trust.app}")
    if not report.url:
        why.append("has no link to its run")
    if why:
        ignored.append("untrusted: control-change report " + "; ".join(why))
        yield Flag(
            FlagKind.CONTROL_CHANGE,
            subject,
            "no trusted control-change report for this exact revision (" + "; ".join(why) + ")",
        )
    elif report.flagged:
        # The CI job is green whatever it finds; the flag is the only signal.
        yield Flag(
            FlagKind.CONTROL_CHANGE,
            subject,
            "CI flagged a control change: "
            + ("; ".join(report.reasons) or "no reason given")
            + f" ({report.url})",
        )


# Comments that switch a check off for a line or a whole file.
_SUPPRESSION_RE = re.compile(
    r"@ts-nocheck|@ts-ignore|@ts-expect-error|eslint-disable(?:-next-line|-line)?|"
    r"(?:istanbul|c8|v8)\s+ignore|biome-ignore|prettier-ignore|oxlint-disable|"
    r"tslint:disable|deno-lint-ignore"
)


def _suppression_flags(candidate, head, base):
    """Check suppressions added or moved in any changed file whose text was
    supplied. Each one is compared together with the line after it (what it
    switches off), so moving one onto new code counts. A file with no text at
    the merge base (new, or not supplied) has every one counted."""
    for path in candidate.changed_paths:
        after = head.get(path)
        if after is None or after.text is None:
            continue
        before = base.get(path)
        left = Counter(k for k, _ in _suppressions(before.text if before and before.text else ""))
        added = []
        for key, line in _suppressions(after.text):
            if left[key] > 0:
                left[key] -= 1
            else:
                added.append(f"line {line}: {key}")
        if added:
            yield Flag(
                FlagKind.SUPPRESSION,
                path,
                "adds or moves comments that switch checks off, so passing typecheck, lint "
                "or coverage may no longer mean what it did: " + " | ".join(added[:_DIFF_LINES]),
            )


def _suppressions(text: str) -> list[tuple[str, int]]:
    """(the suppression's line and the next non-blank line, line number) for
    each line holding a suppression comment."""
    lines = text.replace("\r\n", "\n").split("\n")
    out = []
    for i, x in enumerate(lines):
        if not _SUPPRESSION_RE.search(x):
            continue
        j = i + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        nxt = lines[j] if j < len(lines) else ""
        out.append((_normalize(x) + " / " + _normalize(nxt), i + 1))
    return out


def _is_test_path(path: str) -> bool:
    return path.startswith("tests/") or bool(_TEST_NAME_RE.search(path))


def _test_change_flags(candidate, head, base, parse):
    for path in candidate.changed_paths:
        if not _is_test_path(path):
            continue
        before, after = base.get(path), head.get(path)
        if before is None or after is None:
            missing = " and ".join(
                w for w, s in (("its merge base", before), ("the candidate", after)) if s is None
            )
            yield Flag(
                FlagKind.CHANGED_TEST,
                path,
                f"test file text at {missing} was not supplied, so removed or weakened "
                "assertions cannot be ruled out",
            )
            continue
        if before.text is None:
            continue  # a brand-new file removes nothing
        if after.text is None:
            parsed = parse(before)
            n = len(parsed.tests) if isinstance(parsed, _File) else "its"
            yield Flag(FlagKind.DELETED_TEST, path, f"deletes the test file and {n} tests")
            continue
        if before.text == after.text:
            continue
        old, new = parse(before), parse(after)
        if isinstance(old, str) or isinstance(new, str):
            yield Flag(
                FlagKind.CHANGED_TEST,
                path,
                f"changes an existing test file; {old if isinstance(old, str) else new}",
            )
            continue
        yield from _compare_tests(path, old, new)


def _compare_tests(path, old: _File, new: _File):
    def by_name(f: _File):
        out: dict[str | None, list[_Test]] = {}
        for t in f.tests:
            out.setdefault(t.name, []).append(t)
        return out

    olds, news = by_name(old), by_name(new)
    if any(n is None or len(ts) > 1 for n, ts in [*olds.items(), *news.items()]):
        yield Flag(
            FlagKind.CHANGED_TEST,
            path,
            "changes an existing test file whose test names are not unique or not plain "
            "text, so changes cannot be matched test by test",
        )
    for name, ts in olds.items():
        if name is None or len(ts) > 1 or len(news.get(name, ())) > 1:
            continue
        subject = f"{path}{_SEP}{name}"
        t_old = ts[0]
        if name not in news:
            yield Flag(
                FlagKind.DELETED_TEST,
                subject,
                f"removes or renames the test {name!r} and its "
                f"{_assertion_count(old, t_old)} assertion(s)",
            )
            continue
        t_new = news[name][0]
        before, after = _assertion_count(old, t_old), _assertion_count(new, t_new)
        weaker = []
        if after < before:
            weaker.append(f"{before} assertion(s) down to {after}")
        if t_new.skipped and not t_old.skipped:
            weaker.append("now skipped")
        if t_new.conditional and not t_old.conditional:
            weaker.append("now runs only conditionally")
        if t_new.unregistered and not t_old.unregistered:
            weaker.append("may no longer be registered: " + "; ".join(t_new.unregistered))
        if new.has_only and not old.has_only:
            weaker.append("the file now uses .only, so other tests may not run")
        if weaker:
            yield Flag(FlagKind.WEAKENED_TEST, subject, f"weakens {name!r}: " + "; ".join(weaker))
        elif _body_text(old, t_old) != _body_text(new, t_new):
            yield Flag(
                FlagKind.CHANGED_TEST,
                subject,
                f"changes the existing test {name!r}; an edit can weaken a test without "
                "removing an assertion",
            )
    known = _suite_paths(old)
    product = {name for name, spec in new.imports if _is_product(path, spec)}
    old_imports, old_setup = _outside_tests(old, known, product)
    new_imports, new_setup = _outside_tests(new, known, product)
    reaching = [
        f"+{t.name}: {line}"
        for t in new.tests
        if t.name not in olds
        for line in _new_test_writes(new, t)
    ]
    if reaching:
        yield Flag(
            FlagKind.CHANGED_SETUP,
            path,
            "a new test patches globals, prototypes or modules, or changes a value it does "
            "not declare itself, which can change what the other tests in the file check: "
            + " | ".join(reaching[:_DIFF_LINES]),
        )
    if old_imports != new_imports:
        yield Flag(
            FlagKind.CHANGED_TEST,
            path,
            "changes the imports of an existing test file: " + _line_diff(old_imports, new_imports),
        )
    if old_setup != new_setup:
        # Kept apart from the import change an honest task always makes, so clearing
        # that never clears a new hook, a changed shared value or a changed helper.
        yield Flag(
            FlagKind.CHANGED_SETUP,
            path,
            "changes an existing test file outside its tests (shared values, hooks, helpers "
            "or setup), which can change what every test in it checks: "
            + _line_diff(old_setup, new_setup),
        )
    if new.problems and set(new.problems) != set(old.problems):
        yield Flag(
            FlagKind.WEAKENED_TEST,
            path,
            "the file can no longer be read reliably: " + "; ".join(new.problems),
        )


def _suite_paths(f: _File) -> set[tuple[str | None, ...]]:
    return {_suite_path(f, i) for i, b in enumerate(f.blocks) if b.is_suite}


def _suite_path(f: _File, i: int) -> tuple[str | None, ...]:
    out = []
    p: int | None = i
    while p is not None:
        out.append(f.blocks[p].title)
        p = f.blocks[p].parent
    return tuple(reversed(out))


# A new test or suite body that patches globals, prototypes or modules can change
# what other tests in the file check.
_PATCH_RE = re.compile(
    r"(?<![\w$])(?:prototype|__proto__|eval|Function)(?![\w$])"
    r"|(?<![\w$.])(?:vi|vitest)\s*\.\s*(?:spyOn|stubGlobal|stubEnv|mock|doMock|"
    r"useFakeTimers|setSystemTime)(?![\w$])"
    r"|(?<![\w$.])(?:Object|Reflect)\s*\.\s*(?:defineProperty|defineProperties|assign|"
    r"setPrototypeOf|set|deleteProperty)(?![\w$])"
)
# A write to ``name``, ``name.x``, ``name[...]`` (and deeper), or a mutating call
# on it. Group 1 is the name.
_MEMBER = r"(?:\s*(?:\?\.|\.)\s*[\w$]+|\s*\[[^\]\n]*\])"
_NAME_WRITE_RE = re.compile(
    rf"(?<![\w$.])([A-Za-z_$][\w$]*)(?:{_MEMBER}*\s*(?:[-+*/%&|^]|\*\*|\?\?|&&|\|\|)?"
    rf"=(?![=>])|\s*(?:\+\+|--)|{_MEMBER}*\s*\.\s*(?:push|pop|shift|unshift|splice|sort|"
    rf"reverse|fill|copyWithin|set|delete|clear|add)\s*\()"
)
_LOCAL_RES = (
    re.compile(r"(?<![\w$.])(?:const|let|var|function\s*\*?|class)\s+([A-Za-z_$][\w$]*)"),
    re.compile(r"(?<![\w$.])(?:const|let|var)\s*[\[{]([^=]*)="),
    re.compile(r"\(([^()]*)\)\s*(?::[^=>{]*)?=>"),
    re.compile(r"(?<![\w$.])([A-Za-z_$][\w$]*)\s*=>"),
    re.compile(r"(?<![\w$.])(?:function\s*\*?\s*[\w$]*|catch)\s*\(([^()]*)\)"),
)
_FIXTURE_RE = re.compile(r"\s*(?:const|let|var)\s+[\w$]+\s*(?::[^=]*)?=(?![=>])")
_WRITE_RE = re.compile(r"(?<![=!<>])=(?![=>])|\+\+|--|(?<![\w$])delete(?![\w$])")
# Calls a plain fixture may make besides product code: they read and build values.
_PURE_CALLS = frozenset(
    {
        "structuredClone", "map", "filter", "slice", "concat", "flatMap", "flat", "find",
        "findIndex", "findLast", "some", "every", "includes", "indexOf", "join", "at",
        "keys", "values", "entries", "toSorted", "toReversed", "getTime", "toISOString",
        "Date", "UTC", "parse", "stringify", "String", "Number", "Boolean", "from", "of",
        "trim", "toLowerCase", "toUpperCase", "startsWith", "endsWith", "padStart",
        "padEnd", "repeat", "split", "fromEntries", "min", "max", "round", "floor", "ceil",
    }
)  # fmt: skip


def _new_test_writes(f: _File, t: _Test) -> list[str]:
    """Lines where a new test patches globals, prototypes or modules, or writes to
    a name it does not declare itself (a shared fixture or a global)."""
    a, e = t.block.start, t.block.end
    body = f.masked[a:e]
    local = set()
    for rx in _LOCAL_RES:
        for m in rx.finditer(body):
            local.update(_idents(m.group(1)))
    hits = [m.start() for m in _PATCH_RE.finditer(f.masked, a, e)]
    hits += [
        m.start()
        for m in _NAME_WRITE_RE.finditer(f.masked, a, e)
        if m.group(1) not in local
        and m.group(1) not in _KEYWORDS_BEFORE_EXPR
        and m.group(1) not in ("const", "let", "var")
        and _before(f.masked, m.start()) != ":"  # a type annotation
    ]
    out: dict[int, str] = {}
    for pos in sorted(hits):
        start = f.text.rfind("\n", 0, pos) + 1
        if start not in out:
            out[start] = _normalize(f.text[start : _line_end(f.text, pos)])
    return list(out.values())


def _line_end(text: str, i: int) -> int:
    j = text.find("\n", i)
    return len(text) if j < 0 else j


def _plain_fixture(chunk: str, product: set[str]) -> bool:
    """A ``const``/``let`` whose value writes nothing, patches nothing and calls
    only product code or plain value-building functions."""
    m = _FIXTURE_RE.match(chunk)
    if not m:
        return False
    rhs = chunk[m.end() :]
    if _WRITE_RE.search(rhs) or _PATCH_RE.search(rhs):
        return False
    for k, c in enumerate(rhs):
        if c != "(":
            continue
        j = k - 1
        while j >= 0 and rhs[j] in _WS:
            j -= 1
        if j < 0:
            continue
        p = rhs[j]
        if p.isalnum() or p in "_$":
            word, _ = _word_before(rhs, j, 0)
            if word in product or word in _PURE_CALLS:
                continue
            if word in ("typeof", "void", "in", "of", "instanceof", "return", "await", "new"):
                continue
            return False
        if p == ">" and j > 0 and rhs[j - 1] == "=":
            continue  # an arrow function's parenthesized body
        if p == "!" and not (j > 0 and (rhs[j - 1].isalnum() or rhs[j - 1] in "_$)]")):
            continue  # negation
        if p in ".)]>!`":
            return False  # ?.(, f()(, a[0](, f<T>(, f!(, tagged templates
    return True


def _outside_tests(
    f: _File, known: set[tuple[str | None, ...]], product: set[str]
) -> tuple[list[str], list[str]]:
    """The file's import lines and its other lines, without comments and with
    every test's own call removed. A suite that is not in ``known`` (the base's
    suites) is new: of its body, only plain fixtures (see ``_plain_fixture``) are
    left out, because they only reach the new tests in it; every other statement
    there runs while tests are collected and is kept."""
    n = len(f.text)
    drop = bytearray(n)
    for a, e in f.comments:
        drop[a:e] = b"\x01" * (e - a)
    fresh = []
    for i, b in enumerate(f.blocks):
        if b.is_suite and (b.title is None or _suite_path(f, i) in known):
            continue
        drop[b.start : b.end + 1] = b"\x01" * (b.end + 1 - b.start)
        if b.is_suite:
            fresh.append(i)
    in_import = bytearray(n)
    for a, e in f.import_spans:
        k = _skip_ws(f.masked, e)
        if k < len(f.masked) and f.masked[k] == ";" and "\n" not in f.text[e:k]:
            e = k + 1
        in_import[a:e] = b"\x01" * (e - a)
    imports, other = [], []
    for i, c in enumerate(f.text):
        if c == "\n":
            imports.append(c)
            other.append(c)
        elif not drop[i]:
            (imports if in_import[i] else other).append(c)

    def lines(chars: list[str]) -> list[str]:
        return [_normalize(x) for x in "".join(chars).split("\n") if x.strip(" \t\r;")]

    setup = lines(other)
    for i in fresh:
        setup += _suite_setup(f, i, product)
    return lines(imports), setup


def _suite_setup(f: _File, i: int, product: set[str]) -> list[str]:
    """The statements of new suite ``i``'s own body that are not plain fixtures."""
    b = f.blocks[i]
    if b.body is None:
        return [_normalize(f.text[b.start : b.end + 1])]
    a, e = b.body
    skip = bytearray(e - a)
    for c in f.blocks:
        if c.parent == i or (not c.is_suite and c.start >= a and c.end < e):
            skip[c.start - a : c.end + 1 - a] = b"\x01" * (c.end + 1 - c.start)
    for x, y in f.comments:
        lo, hi = max(x, a), min(y, e)
        if lo < hi:
            skip[lo - a : hi - a] = b"\x01" * (hi - lo)
    out, masked, text = [], [], []
    depth = 0

    def flush() -> None:
        m, t = "".join(masked), _normalize("".join(text))
        if t.strip(";") and not _plain_fixture(m, product):
            out.append(t)
        masked.clear()
        text.clear()

    for k in range(a, e):
        if skip[k - a]:
            continue
        c = f.masked[k]
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        if depth <= 0 and c in ";\n":
            flush()
            continue
        masked.append(c)
        text.append(f.text[k])
    flush()
    return out


_DIFF_LINES = 40


def _line_diff(before: list[str], after: list[str]) -> str:
    """Every removed (-) and added (+) line, in order, up to a limit. Lines are
    compared as a multiset, so a line that only moved is not listed."""
    gone, came = Counter(before), Counter(after)
    gone, came = gone - Counter(after), came - Counter(before)
    out = []
    for sign, lines, extra in (("-", before, gone), ("+", after, came)):
        for x in lines:
            if extra[x] > 0:
                extra[x] -= 1
                out.append(f"{sign}{x}")
    if not out:
        out = [f"moved: {y}" for x, y in zip(before, after, strict=False) if x != y]
    more = len(out) - _DIFF_LINES
    shown = " | ".join(out[:_DIFF_LINES])
    return shown + (f" | ... and {more} more changed line(s)" if more > 0 else "")


def _body_text(f: _File, t: _Test) -> str:
    b = t.block
    return _normalize(f.text[b.start : b.end + 1])


def _criterion(c, v, candidate, links, head, parse, proofs, limits, policy, ignored):
    cid, ev = c["id"], c["evidence"]
    base_entry = dict(
        criterion=cid,
        statement=c["statement"],
        evidence_type=ev["type"],
        commit=candidate.head_commit,
    )
    if v is None:
        return CriterionCoverage(
            status=Coverage.UNKNOWN,
            reasons=("the criterion check has no verdict for it",),
            **base_entry,
        ), []

    if ev["type"] != "automated-check":
        if v.verdict is Verdict.PASS:
            return CriterionCoverage(
                status=Coverage.COVERED,
                observations=v.citations,
                limits=tuple(x.limitations for x in v.citations if x.limitations),
                **base_entry,
            ), []
        return CriterionCoverage(
            status=Coverage.FAILED if v.verdict is Verdict.FAIL else Coverage.UNKNOWN,
            reasons=v.reasons or ("no independent observation shows it on this revision",),
            **base_entry,
        ), []

    usable = []
    for link in (x for x in links if x.criterion == cid):
        label = f"link of {cid} to {link.path}{_SEP}{link.test} by {link.mapper or 'nobody'}"
        if not policy.independent(link.mapper):
            ignored.append(f"untrusted: {label}: the worker's own mapping is not evidence")
        elif link.commit != candidate.head_commit or link.base_commit != candidate.base_commit:
            ignored.append(f"stale: {label} was made on {link.commit[:12]}/{link.base_commit[:12]}")
        elif not link.why.strip():
            ignored.append(f"incomplete: {label} does not say how the assertion shows {cid}")
        else:
            usable.append(link)
    if not usable:
        return CriterionCoverage(
            status=Coverage.UNCOVERED,
            reasons=(
                "no independent mapping names an assertion that checks it on this revision "
                "(a passing suite does not show which test, if any, checks it)",
            ),
            **base_entry,
        ), []

    mapped, problems, link_flags = [], [], []
    for link in usable:
        result, link_problems, fl = _check_link(link, ev["command"], head, parse, policy)
        if result is None:
            problems += [f"{link.path}{_SEP}{link.test}: {p}" for p in link_problems]
        else:
            mapped.append((link, result))
            link_flags += [replace(f, criterion=cid) for f in fl]
    if not mapped:
        return CriterionCoverage(status=Coverage.UNKNOWN, reasons=tuple(problems), **base_entry), []
    ignored += [f"link of {cid} not used: {p}" for p in problems]

    entry_limits = [
        f"{m.path}{_SEP}{m.test}: the suite passed on this commit and the test is "
        "registered unconditionally in source, but no per-test result was recorded"
        for _, m in mapped
    ]
    proof_limits, proof_flags = _failure_proof(
        cid, mapped, candidate, proofs, limits, policy, ignored
    )
    entry_limits += proof_limits
    link_flags += proof_flags

    assertions = tuple(m for _, m in mapped)
    if v.verdict is Verdict.PASS:
        status, reasons = Coverage.COVERED, ()
    elif v.verdict is Verdict.FAIL:
        status, reasons = Coverage.FAILED, v.reasons
    else:
        status = Coverage.UNKNOWN
        reasons = v.reasons or (f"`{ev['command']}` has no trusted passing result",)
    return CriterionCoverage(
        status=status,
        assertions=assertions,
        limits=tuple(entry_limits),
        reasons=tuple(reasons),
        **base_entry,
    ), link_flags


def _suite_path_ok(path: str, suite: str) -> bool:
    parts = path.split("/")
    return bool(re.fullmatch(suite, path)) and not any(
        p in ("", ".", "..", "node_modules") for p in parts
    )


def _check_link(link: AssertionLink, command: str, head, parse, policy):
    """(MappedAssertion or None, problems, flags) for one link."""
    suite = policy.suite_for(command)
    if suite is None:
        return None, [f"which test files `{command}` runs is not known"], []
    if not _suite_path_ok(link.path, suite):
        return None, [f"`{command}` does not run {link.path}"], []
    src = head.get(link.path)
    if src is None:
        return None, ["its text at the candidate commit was not supplied"], []
    if src.text is None:
        return None, ["the file does not exist at the candidate commit"], []
    f = parse(src)
    if isinstance(f, str):
        return None, [f], []
    if f.problems:
        return None, list(f.problems), []
    matches = [t for t in f.tests if t.name == link.test]
    if not matches:
        return None, ["no test with that full name"], []
    if len(matches) > 1:
        return None, ["more than one test has that full name"], []
    t = matches[0]
    fns = {t.block.fn, "vi"}
    p = t.block.parent
    while p is not None:
        fns.add(f.blocks[p].fn)
        p = f.blocks[p].parent
    problems = list(t.unregistered)
    if t.skipped:
        problems.append("the test is skipped, todo, expected to fail or has no body")
    if t.conditional:
        problems.append("the test runs only when a condition holds (skipIf/runIf)")
    if t.parameterized:
        problems.append("parameterized tests (each/for) are not supported; map a plain test")
    if f.has_only and not t.only:
        problems.append("the file uses .only, so this test may not run")
    if problems:
        return None, problems, []

    s, e = t.block.body
    nested = [(b.start, b.end) for b in f.blocks if b is not t.block and s <= b.start < e]
    found = None
    for pos, quote_end in _locate(f, s, e, link.assertion):
        a = _assertion_at(f, pos, quote_end)
        if a is None or a.end > e or any(x <= a.start <= y for x, y in nested):
            continue
        found = a
        break
    if found is None:
        return (
            None,
            [
                "the quoted assertion is not in that test's own body as a complete "
                "expect(...).matcher(...) or assert(...) call"
            ],
            [],
        )
    a = found
    fns.add(a.call.split(".")[0])
    shadowed = [why for name, why in f.shadowed if name in fns]
    if shadowed:
        return None, [f"{why}, so this test cannot be trusted" for why in shadowed], []
    [(stack, exits)] = _walk(f, s, [a.start])
    hard_exits = [ln for ln, cond in exits if not cond]
    if hard_exits:
        return None, [f"line {hard_exits[0]}: returns before the assertion"], []

    subject = f"{link.path}{_SEP}{link.test}"
    flags = []
    for ln, _ in exits:
        flags.append(
            Flag(
                FlagKind.MAY_NOT_RUN,
                subject,
                f"line {ln}: a conditional return before the assertion can skip it",
            )
        )
    if "fn" in stack:
        flags.append(
            Flag(
                FlagKind.MAY_NOT_RUN,
                subject,
                "the assertion is inside a callback; confirm the callback is called",
            )
        )
    elif stack:
        flags.append(
            Flag(
                FlagKind.MAY_NOT_RUN,
                subject,
                "the assertion is inside a condition, loop or block; confirm it runs",
            )
        )
    guard = _guard(f, s, a.start)
    if guard:
        flags.append(Flag(FlagKind.MAY_NOT_RUN, subject, f"the assertion {guard}"))

    product = {name for name, spec in f.imports if _is_product(link.path, spec)}
    namespaces = set(f.namespaces) & product
    bindings = _bindings(f, t)
    aliases = _aliases(product, namespaces, bindings)
    if _is_literal(a.subject):
        flags.append(Flag(FlagKind.FIXTURE_ONLY, subject, "the assertion checks a literal value"))
    elif not (
        _traced(a.subject, product, bindings)
        or _passed_to_product(f, s, a.start, a.subject, product | set(aliases), namespaces)
    ):
        flags.append(
            Flag(
                FlagKind.FIXTURE_ONLY,
                subject,
                "the asserted value is not traced to code imported from the product, so the "
                "test may only check its own fixtures",
            )
        )
    mocked = sorted({m for m in f.mocks if _is_product(link.path, m)})
    if mocked:
        flags.append(
            Flag(
                FlagKind.FIXTURE_ONLY, subject, "the file mocks product code: " + ", ".join(mocked)
            )
        )
    spied = sorted({s for s in f.spies if s == "?" or set(_idents(s)) & product})
    if spied:
        flags.append(
            Flag(
                FlagKind.FIXTURE_ONLY,
                subject,
                "the file replaces product code with vi.spyOn or a stub: " + ", ".join(spied),
            )
        )

    weak = _weakness(a)
    if weak:
        flags.append(Flag(FlagKind.WEAK_ASSERTION, subject, weak))

    if a.expected is not None:
        if _normalize(a.subject) and _normalize(a.subject) == _normalize(a.expected):
            flags.append(
                Flag(FlagKind.MIRRORS_CODE, subject, "the assertion compares a value with itself")
            )
        primary = _calls(a.subject, product, namespaces, aliases)
        expected = _calls(a.expected, product, namespaces, aliases) | {
            n
            for name in _idents(a.expected)
            for v in bindings.get(name, ())
            for n in _calls(v, product, namespaces, aliases)
        }
        computed = _computed(a.expected, bindings)
        if computed:
            flags.append(
                Flag(
                    FlagKind.MIRRORS_CODE,
                    subject,
                    f"the expected value is worked out with `.{computed}(...)` instead of "
                    "written out, so it may repeat the code's own logic and agree with it "
                    "whatever it does",
                )
            )
        mirrored = primary & expected
        if mirrored:
            flags.append(
                Flag(
                    FlagKind.MIRRORS_CODE,
                    subject,
                    "the expected value is computed by the same product code it checks ("
                    + ", ".join(sorted(mirrored))
                    + "), so the test agrees with the code whatever it does",
                )
            )

    return (
        MappedAssertion(
            path=link.path,
            test=link.test,
            line=f.line(a.start),
            assertion=_normalize(f.text[a.start : a.end]),
            commit=src.commit,
            mapper=link.mapper,
            why=link.why,
        ),
        [],
        flags,
    )


_COMPUTING_RE = re.compile(
    r"\.\s*(filter|map|flatMap|reduce|reduceRight|find|findLast|findIndex|findLastIndex|"
    r"some|every|sort|toSorted|forEach)\s*\("
)


def _computed(expected: str, bindings) -> str | None:
    """The array method an expected value is worked out with, directly or through
    a variable it names, if any."""
    for expr in (expected, *(v for n in _idents(expected) for v in bindings.get(n, ()))):
        m = _COMPUTING_RE.search(expr)
        if m:
            return m.group(1)
    return None


def _passed_to_product(f, start, pos, subject, product, namespaces) -> bool:
    """Whether a name in the asserted value was handed to product code earlier in
    the test, as in "does not change its input": expect(books).toEqual(before)."""
    names = set(_idents(subject)) - product
    if not names:
        return False
    for m in _CALL_SITE_RE.finditer(f.masked, start, pos):
        a, b = m.group(1), m.group(2)
        if not ((a in namespaces and b) or (not b and a in product)):
            continue
        try:
            args = f.masked[m.end() : _match(f.masked, m.end() - 1)]
        except ParseError:
            continue
        if names & set(_idents(args)):
            return True
    return False


def _weakness(a: _Assertion) -> str | None:
    op = _top_level_operator(a.subject)
    if op:
        return f"the asserted value is combined with other values by `{op}`"
    if a.expected is None:
        if a.call in _WEAK_ASSERTS:
            return f"`{a.call}(...)` only checks that the value is truthy"
        return None
    if a.negated:
        return "a negated matcher passes for almost any wrong value"
    if a.matcher in _WEAK_MATCHERS:
        return f"`{a.matcher}` does not pin the value down"
    if a.matcher in _BOUND_MATCHERS:
        return f"`{a.matcher}` only checks a bound, so many wrong values pass"
    if a.matcher in ("toThrow", "toThrowError") and not a.expected.strip():
        return f"`{a.matcher}()` with no expected error passes on any error"
    if a.matcher == "toHaveProperty" and len(_split_args(a.expected)) < 2:
        return "`toHaveProperty` without a value only checks the property exists"
    if a.matcher == "toMatchObject" and re.fullmatch(r"\s*\{\s*\}\s*", a.expected):
        return "`toMatchObject({})` matches any object"
    asym = re.search(r"(?<![\w$.])expect\s*\.\s*(?:not\s*\.\s*)?([A-Za-z]+)\s*\(", a.expected)
    if asym:
        return f"the expected value uses expect.{asym.group(1)}; confirm it pins the value down"
    return None


def _failure_proof(cid, mapped, candidate, proofs, limits, policy, ignored):
    names = {(link.path, link.test) for link, _ in mapped}
    good, passed = [], []
    for p in proofs:
        if p.criterion != cid:
            continue
        label = f"failure proof of {cid} ({p.path}{_SEP}{p.test}) by {p.by or 'nobody'}"
        if (p.path, p.test) not in names:
            ignored.append(f"unused: {label} is for a test no usable link names")
        elif not policy.independent(p.by):
            ignored.append(f"untrusted: {label}: the worker's own run is not evidence")
        elif p.tests_commit != candidate.head_commit or p.code_commit != candidate.merge_base:
            ignored.append(
                f"stale: {label} ran tests from {p.tests_commit[:12]} on code from "
                f"{p.code_commit[:12]}, not the candidate's tests on its merge base"
            )
        elif not p.url:
            ignored.append(f"untrusted: {label} has no link to its run")
        elif p.outcome is ProofOutcome.PASSED:
            passed.append(p)
        else:
            good.append(p)
    flags = []
    for p in passed:
        conflict = any((g.path, g.test) == (p.path, p.test) for g in good)
        flags.append(
            Flag(
                FlagKind.PASSES_WITHOUT_CHANGE,
                cid,
                f"{p.path}{_SEP}{p.test} passed on the base commit's code ({p.url})"
                + (", while another run says it failed" if conflict else "")
                + ", so it may not detect the original failure",
                criterion=cid,
            )
        )
    if any(p.outcome is ProofOutcome.FAILED_ASSERTION for p in good):
        return [], flags
    if good:
        p = good[0]
        return [
            f"{p.path}{_SEP}{p.test} failed without the change, but before reaching its "
            f"assertion ({p.url}): that shows the test needs the new code, not that its "
            "assertion detects wrong behavior"
        ], flags
    if flags:
        return [], flags
    noted = []
    for lim in limits:
        if lim.criterion != cid:
            continue
        label = f"failure-proof limit for {cid} by {lim.by or 'nobody'}"
        if not policy.independent(lim.by):
            ignored.append(f"untrusted: {label}: the worker cannot excuse its own test")
        elif lim.commit != candidate.head_commit or lim.base_commit != candidate.base_commit:
            ignored.append(f"stale: {label} was made on {lim.commit[:12]}")
        elif not lim.reason.strip():
            ignored.append(f"incomplete: {label} gives no reason")
        else:
            noted.append(f"not shown to fail without the change: {lim.reason} ({lim.by})")
    if noted:
        return noted, []
    return [], [
        Flag(
            FlagKind.NO_FAILURE_PROOF,
            cid,
            "no run shows the linked test failing without the change, and no reason is "
            "recorded why that is not practical; the test may only mirror the new code",
            criterion=cid,
        )
    ]


def _apply_clearances(flags, clearances, candidate, coverage, policy, ignored):
    gaps = {c.criterion for c in coverage if c.status is not Coverage.COVERED}
    out = dict(flags)
    for cl in clearances:
        label = f"clearance of {cl.flag!r} by {cl.by or 'nobody'}"
        kind, _, target = cl.flag.partition(":")
        if cl.flag not in out:
            if cl.flag in gaps or target in gaps or kind in ("uncovered", "unknown", "failed"):
                ignored.append(
                    f"refused: {label}: an uncovered, unknown or failed criterion cannot be "
                    "cleared; revise the contract and approve it again"
                )
            else:
                ignored.append(f"unused: {label} matches no flag on this revision")
            continue
        if not policy.is_reviewer(cl.by):
            ignored.append(f"untrusted: {label}: only {', '.join(sorted(policy.reviewers))} may")
        elif cl.commit != candidate.head_commit or cl.base_commit != candidate.base_commit:
            ignored.append(f"stale: {label} was for {cl.commit[:12]}/{cl.base_commit[:12]}")
        elif not cl.note.strip():
            ignored.append(f"incomplete: {label} does not say what was checked")
        elif out[cl.flag].cleared_by is None:
            out[cl.flag] = replace(out[cl.flag], cleared_by=cl.by, note=cl.note)
    return tuple(out.values())


# --- Rendering ------------------------------------------------------------------


def render(report: AssertionReport) -> str:
    """The report as Markdown, for a PR comment or Rolando's review.

    Every supplied string is put on one line and has its Markdown and HTML
    escaped (or sits in a code span), so nothing in a test name, note or reason
    can start a line of its own, such as a fake "ready" line, or hide the rest
    of the report.
    """
    o = _md
    lines = [
        "## Assertion map",
        "",
        f"- Contract digest: `{report.contract_digest}`",
        f"- Candidate commit: `{report.candidate_commit}`",
        f"- Base commit: `{report.base_commit}`",
        f"- Ready for Rolando's review: **{'yes' if report.ready else 'no'}** "
        "(this is not an approval)",
        "",
        "| Criterion | Status | Shown by | Tested commit |",
        "|---|---|---|---|",
    ]
    for c in report.criteria:
        if c.status is Coverage.COVERED and c.assertions:
            shown = "; ".join(
                f"{o(a.path)}:{a.line} {_code(a.test)} {_code(a.assertion)}" for a in c.assertions
            )
        elif c.status is Coverage.COVERED:
            shown = o("; ".join(x.detail.splitlines()[0] for x in c.observations if x.detail))
        else:
            shown = o("; ".join(c.reasons))
        lines.append(
            f"| {o(c.criterion)} | {c.status.value} | {_cell(shown)} | `{c.commit[:12]}` |"
        )
    if report.flags:
        lines += ["", "### Needs review", ""]
        for f in report.flags:
            state = f"cleared by {o(f.cleared_by)}: {o(f.note)}" if f.cleared_by else "open"
            lines.append(f"- {_code(f.key)} ({state}): {o(f.detail)}")
    lines += ["", "### Gates", ""]
    lines += [f"- {'ok' if g.ok else 'BLOCKED'}: {o(g.name)}: {o(g.detail)}" for g in report.gates]
    lines.append(
        f"- {'ok' if report.verification.ready else 'BLOCKED'}: criterion check "
        "(see its own report)"
    )
    for c in report.criteria:
        if not (c.assertions or c.limits):
            continue
        lines += ["", f"### {o(c.criterion)}: {c.status.value}", "", o(c.statement)]
        for a in c.assertions:
            lines.append(
                f"- {o(a.path)}:{a.line} {_code(a.test)}, mapped by {o(a.mapper)}: {o(a.why)}"
            )
        lines += [f"- Limit: {o(x)}" for x in c.limits]
    if report.ignored:
        lines += ["", "### Evidence not used", ""]
        lines += [f"- {o(i)}" for i in report.ignored]
    return "\n".join(lines) + "\n"


_LINE_BREAKS = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")


def _one_line(text: str) -> str:
    return _LINE_BREAKS.sub(" ", str(text))


_MD_SPECIAL = re.compile(r"([\\`*_\[\]#~!])")
_HTML = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _md(text: str) -> str:
    """Supplied text made inert: one line, Markdown escaped, HTML as entities."""
    text = _MD_SPECIAL.sub(r"\\\1", _one_line(text))
    return re.sub(r"[&<>]", lambda m: _HTML[m.group(0)], text)


def _code(text: str) -> str:
    text = _one_line(text)
    longest = max((len(m) for m in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


__all__ = [
    "AssertionLink",
    "AssertionPolicy",
    "AssertionReport",
    "Clearance",
    "ControlChangeReport",
    "Coverage",
    "CriterionCoverage",
    "DEFAULT_ASSERTION_POLICY",
    "FailureProof",
    "FailureProofLimit",
    "Flag",
    "FlagKind",
    "MappedAssertion",
    "ProofOutcome",
    "TestSource",
    "map_assertions",
    "render",
]
