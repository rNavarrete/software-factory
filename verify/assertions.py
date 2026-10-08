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
changes; deleted, weakened or changed existing tests; assertions on fixtures
rather than product code; weak matchers; expected values computed by the code
under test; assertions that may not run; and checks never shown to fail
without the change. Each flag holds readiness until a reviewer clears it on
this exact revision.

The reading of test files is deliberately suspicious: anything it cannot
follow with confidence (shadowed or aliased test functions, runtime skips,
options objects that can skip tests, tests registered inside functions or
conditions, unparseable text) makes the link unknown rather than covered. It
is still a heuristic, not a JavaScript engine. A test that mirrors an incorrect
implementation in a way these checks cannot see is possible, which is why the
failure proof and Rolando's review exist.

The module only reads. It does no I/O, changes none of its inputs, and its
report has no way to approve, merge or release: ``ready`` means "ready for
Rolando to review". Standard library only.
"""

from __future__ import annotations

import bisect
import posixpath
import re
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
            "(only a revised contract, approved again, can change what it needs)"
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
                i = n if j < 0 else j
                continue
            if t.startswith("/*", i):
                j = t.find("*/", i + 2)
                if j < 0:
                    raise ParseError("unterminated comment")
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
    re.compile(rf"(?<![\w$])({_GUARDED})\s*(?:[-+*/%&|^]|\*\*|<<|>>>?|&&|\|\||\?\?)?=(?![=>])"),
)
_DESTRUCTURE_RE = re.compile(r"(?<![\w$.])(?:const|let|var)\s*([\[{])")
_SKIP_WORD_RE = re.compile(r"(?<![\w$])skip(?![\w$])")


@dataclass(frozen=True)
class _Block:
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
    """File-level reasons no test in it can be trusted (shadowing, runtime skips)."""
    imports: tuple[tuple[str, str], ...]
    """(local name, module specifier) for every value import."""
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
    imports, import_spans = _imports(text, scan)
    problems: list[str] = []

    def line(pos: int) -> int:
        return bisect.bisect_right(newlines, pos - 1) + 1

    def in_import(pos: int) -> bool:
        return any(a <= pos < b for a, b in import_spans)

    blocks: list[_Block] = []
    suites: list[int] = []  # stack of open suite block indices
    for m in _CALL_RE.finditer(masked):
        if in_import(m.start()):
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
        body, options = _callback_body(masked, k, close)
        if options is not None:
            unsafe = set(_idents(options)) - _SAFE_OPTIONS - _LITERAL_WORDS
            if unsafe:
                problems.append(
                    f"line {line(m.start())}: an options argument ({', '.join(sorted(unsafe))}) "
                    "can skip tests or mark them only or failing"
                )
        while suites and not (
            blocks[suites[-1]].body
            and blocks[suites[-1]].body[0] <= m.start() < blocks[suites[-1]].body[1]
        ):
            suites.pop()
        blocks.append(
            _Block(
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
        text, masked, bytes(scan.code), tuple(blocks), (), False, (), (), (), (), newlines
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

    problems += _shadowing(text, masked, scan, imports)
    heads = [(b.start, b.head_end) for b in blocks]
    for m in _SKIP_WORD_RE.finditer(masked):
        if not any(a <= m.start() < e for a, e in heads):
            problems.append(
                f"line {line(m.start())}: skips tests at runtime (skip outside a test's "
                "declaration)"
            )
            break

    return _File(
        text=text,
        masked=masked,
        code=bytes(scan.code),
        blocks=tuple(blocks),
        tests=tuple(tests),
        has_only=any("only" in b.mods for b in blocks),
        problems=tuple(problems),
        imports=tuple(imports),
        mocks=tuple(_mocks(masked, scan)),
        spies=tuple(_spies(masked)),
        newlines=newlines,
    )


def _callback_body(masked: str, i: int, close: int):
    """(body range or None, options-object text or None) of a test call's arguments."""
    depth, options = 0, None
    while i < close:
        c = masked[i]
        if depth == 0 and c == "{":
            end = _match(masked, i)
            options = (
                masked[i + 1 : end] if options is None else options + " " + masked[i + 1 : end]
            )
            i = end + 1
            continue
        if c in _OPEN:
            depth += 1
        elif c in _CLOSE:
            depth -= 1
        elif depth == 0 and masked.startswith("=>", i):
            q = _skip_ws(masked, i + 2)
            if masked[q] == "{":
                return (q + 1, _match(masked, q)), options
            # Expression body: up to the next top-level comma or the call's close.
            d, e = 0, q
            while e < close:
                if masked[e] in _OPEN:
                    d += 1
                elif masked[e] in _CLOSE:
                    d -= 1
                elif masked[e] == "," and d == 0:
                    break
                e += 1
            return (q, e), options
        elif (
            depth == 0
            and _FUNCTION_RE.match(masked, i)
            and (i == 0 or not (masked[i - 1].isalnum() or masked[i - 1] in "_$"))
        ):
            p = masked.find("(", i)
            if p < 0 or p > close:
                return None, options
            q = _skip_ws(masked, _match(masked, p) + 1)
            if q < close and masked[q] == "{":
                return (q + 1, _match(masked, q)), options
            return None, options
        i += 1
    return None, options


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
    what encloses it and which return/throw statements before it can end the
    enclosing body early.

    Each state is (stack, exits): stack holds "fn", "block" or "paren" for each
    open bracket (plus "fn" for an expression-bodied arrow), exits holds
    (line, conditional) for each return or throw not inside a nested function.
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
                elif word in ("return", "throw") and "fn" not in stack:
                    exits.append((f.line(i), bool(stack), _skip_ws(m, j)))
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


def _guard(f: _File, floor: int, pos: int) -> str | None:
    """Why the statement at ``pos`` may not run even when the code before it
    does: it is not at the start of a statement but behind a condition, a
    loop header or an operator."""
    m = f.masked
    j = pos - 1
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
        if word in ("await", "return", "void"):
            return _guard(f, floor, s)
        if word in ("else", "do"):
            return f"is in an `{word}` branch"
        return None  # the end of the previous statement, without a semicolon
    if c in "]":
        return None
    return f"is part of an expression after `{c}`, so it may not run"


def _shadowing(text, masked, scan, imports) -> list[str]:
    out = []
    for local, spec in imports:
        if re.fullmatch(_GUARDED, local) and spec != "vitest":
            out.append(f"imports {local!r} from {spec!r} instead of vitest")
    for rx in _SHADOW_RES:
        for m in rx.finditer(masked):
            out.append(f"redefines {m.group(1)!r}")
    for m in _DESTRUCTURE_RE.finditer(masked):
        try:
            inner = masked[m.start(1) + 1 : _match(masked, m.start(1))]
        except ParseError:
            continue
        for name in _idents(inner):
            if re.fullmatch(_GUARDED, name):
                out.append(f"redefines {name!r}")
    for m in re.finditer(rf"\.\s*({_GUARDED})\s*=(?![=>])", masked):
        out.append(f"assigns {m.group(1)!r} on an object")
    return [f"{x}, so its test calls cannot be trusted" for x in dict.fromkeys(out)]


_IMPORT_RE = re.compile(
    r"\bimport\s+(?P<type>type\s+)?(?P<clause>[\w$*{}\s,]+?)\s+from\s+(?=[\"'])", re.S
)


def _imports(text: str, scan: _Scanner):
    out, spans = [], []
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
    return out, spans


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


_ASSERT_RE = re.compile(r"(?<![\w$.])(expect(?:\s*\.\s*soft)?|assert(?:\s*\.\s*\w+)?)\s*\(")
_MATCHER_RE = re.compile(
    r"(?P<mods>(?:\s*\.\s*(?:not|resolves|rejects))*)\s*\.\s*(?P<name>\w+)\s*\("
)
_LITERAL_WORDS = frozenset({"true", "false", "null", "undefined", "NaN", "Infinity"})
_WEAK_MATCHERS = frozenset(
    {
        "toBeDefined",
        "toBeUndefined",
        "toBeTruthy",
        "toBeFalsy",
        "toBeNull",
        "toBeNaN",
        "toBeInstanceOf",
        "toBeTypeOf",
        "toHaveBeenCalled",
        "toMatchSnapshot",
        "toMatchInlineSnapshot",
        "toMatchFileSnapshot",
    }
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
    hidden = [
        (b.start, b.end) for i, b in enumerate(f.blocks) if i not in own and b.body is not None
    ]

    def visible(pos: int) -> bool:
        return not any(a <= pos <= e for a, e in hidden)

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
    return out


def _reach(masked_expr: str, product: set[str], bindings, depth: int = 4) -> set[str]:
    """Product names an expression uses, directly or through assigned names."""
    found: set[str] = set()
    seen: set[str] = set()
    todo = [(masked_expr, depth)]
    while todo:
        expr, d = todo.pop()
        for name in _idents(expr):
            if name in product:
                found.add(name)
            elif d > 0 and name in bindings and name not in seen:
                seen.add(name)
                todo += [(v, d - 1) for v in bindings[name]]
    return found


def _called(masked_expr: str, names: set[str]) -> set[str]:
    return {
        n
        for n in names
        if re.search(rf"(?<![\w$.]){re.escape(n)}\s*(?:\.\s*[\w$]+\s*)?\(", masked_expr)
    }


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
    before_lines, after_lines = _outside_tests(old), _outside_tests(new)
    if before_lines != after_lines:
        removed = [x for x in before_lines if x not in after_lines]
        added = [x for x in after_lines if x not in before_lines]
        sample = [f"-{x}" for x in removed[:3]] + [f"+{x}" for x in added[:3]]
        yield Flag(
            FlagKind.CHANGED_TEST,
            path,
            "changes an existing test file outside its tests (imports, shared fixtures, "
            "setup or helpers)" + (": " + " | ".join(sample) if sample else ""),
        )
    if new.problems and set(new.problems) != set(old.problems):
        yield Flag(
            FlagKind.WEAKENED_TEST,
            path,
            "the file can no longer be read reliably: " + "; ".join(new.problems),
        )


def _outside_tests(f: _File) -> list[str]:
    """The file's lines with every test's own call removed (suite headers kept)."""
    parts, i = [], 0
    for b in sorted((b for b in f.blocks if not b.is_suite), key=lambda b: b.start):
        if b.start < i:
            continue
        parts.append(f.text[i : b.start])
        i = b.end + 1
    parts.append(f.text[i:])
    return [_normalize(x) for x in "\n".join(parts).splitlines() if x.strip()]


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
    [(stack, exits)] = _walk(f, s, [a.start])
    hard_exits = [ln for ln, cond in exits if not cond]
    if hard_exits:
        return None, [f"line {hard_exits[0]}: returns or throws before the assertion"], []

    subject = f"{link.path}{_SEP}{link.test}"
    flags = []
    for ln, _ in exits:
        flags.append(
            Flag(
                FlagKind.MAY_NOT_RUN,
                subject,
                f"line {ln}: a conditional return or throw before the assertion can skip it",
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
    bindings = _bindings(f, t)
    reached = _reach(a.subject, product, bindings)
    if _is_literal(a.subject):
        flags.append(Flag(FlagKind.FIXTURE_ONLY, subject, "the assertion checks a literal value"))
    elif not reached:
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
        primary = _called(a.subject, product)
        mirrored = primary & (
            _called(a.expected, product)
            | {
                n
                for name in _idents(a.expected)
                for v in bindings.get(name, ())
                for n in _called(v, primary)
            }
        )
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


def _weakness(a: _Assertion) -> str | None:
    if a.expected is None:
        if a.call in _WEAK_ASSERTS:
            return f"`{a.call}(...)` only checks that the value is truthy"
        return None
    if a.negated:
        return "a negated matcher passes for almost any wrong value"
    if a.matcher in _WEAK_MATCHERS:
        return f"`{a.matcher}` does not pin the value down"
    if a.matcher in ("toThrow", "toThrowError") and not a.expected.strip():
        return f"`{a.matcher}()` with no expected error passes on any error"
    if re.search(r"(?<![\w$.])expect\s*\.\s*(?:any|anything)\s*\(", a.expected):
        return "the expected value uses expect.any/expect.anything"
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

    Every supplied string is put on one line, so nothing in a test name, note
    or reason can start a line of its own (such as a fake "ready" line).
    """
    o = _one_line
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
                f"{a.path}:{a.line} {_code(a.test)} {_code(a.assertion)}" for a in c.assertions
            )
        elif c.status is Coverage.COVERED:
            shown = "; ".join(x.detail.splitlines()[0] for x in c.observations if x.detail)
        else:
            shown = "; ".join(c.reasons)
        lines.append(
            f"| {o(c.criterion)} | {c.status.value} | {_cell(o(shown))} | `{c.commit[:12]}` |"
        )
    if report.flags:
        lines += ["", "### Needs review", ""]
        for f in report.flags:
            state = f"cleared by {o(f.cleared_by)}: {o(f.note)}" if f.cleared_by else "open"
            lines.append(f"- {_code(o(f.key))} ({state}): {o(f.detail)}")
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
                f"- {o(a.path)}:{a.line} {_code(o(a.test))}, mapped by {o(a.mapper)}: {o(a.why)}"
            )
        lines += [f"- Limit: {o(x)}" for x in c.limits]
    if report.ignored:
        lines += ["", "### Evidence not used", ""]
        lines += [f"- {o(i)}" for i in report.ignored]
    return "\n".join(lines) + "\n"


_LINE_BREAKS = re.compile(r"[\r\n\v\f\x1c-\x1e\x85\u2028\u2029]+")


def _one_line(text: str) -> str:
    return _LINE_BREAKS.sub(" ", str(text))


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
