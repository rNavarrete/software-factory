"""Independent verification of a candidate against its approved contract.

``verify.criteria`` (ENG-156) gives each acceptance criterion a pass, fail or
unknown verdict from evidence the worker did not produce.
"""

from verify.criteria import (
    Candidate,
    CheckResult,
    Citation,
    CriterionVerdict,
    Gate,
    Observation,
    Report,
    Source,
    TrustPolicy,
    Verdict,
    WriterClaim,
    render,
    results_from_check_evidence,
    verify,
)

__all__ = [
    "Candidate",
    "CheckResult",
    "Citation",
    "CriterionVerdict",
    "Gate",
    "Observation",
    "Report",
    "Source",
    "TrustPolicy",
    "Verdict",
    "WriterClaim",
    "render",
    "results_from_check_evidence",
    "verify",
]
