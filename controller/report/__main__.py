"""``python3 -m controller.report probe <issue-id>``: check the live path once.

Posts one test comment on the given ticket (by key, like ``ENG-178``, or
UUID), then posts it twice more under the same key, and reads the ticket
back. It passes when Linear shows exactly one comment, by the factory's own
user with no bot attached, whose marker lines came back unchanged. That
proves Linear accepts the factory's comment ids, a retried message never
shows twice, and replies to the factory's questions can be recognised. It
also fails if the key acts as Rolando. It writes that one comment only.

Run it on the machine, with the ``linear-key`` secret, before switching the
service to ``--reporter linear``.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime

from controller.report.linear_api import HttpTransport
from controller.report.messages import QUESTION_MARK, Stage, progress
from controller.report.replies import LinearDecisions, own_markers
from controller.report.reporter import QUESTION_PREFIX, LinearReporter, comment_id
from controller.service.main import ROLANDO_LINEAR_ID
from controller.service.seams import ReportFailed
from controller.service.secrets import FileSecrets


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m controller.report")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("probe")
    s.add_argument("issue_id")
    s.add_argument("--approver-linear-id", default=ROLANDO_LINEAR_ID)
    args = p.parse_args(sys.argv[1:] if argv is None else argv)

    secrets = FileSecrets(os.environ.get("FACTORY_SECRETS_DIR", "/run/factory-secrets"))
    transport = HttpTransport(lambda: secrets.get("linear-key"))
    reporter = LinearReporter(transport, args.approver_linear_id)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    key = f"{QUESTION_PREFIX}probe-{stamp}"
    text = (
        progress(Stage.NOTICE, "A one-off check that the factory can post here. Ignore it.")
        + f"\n\n{QUESTION_MARK} probe options=-"
    )
    try:
        issue = reporter.issue_uuid(args.issue_id)
        reporter.post(issue, key, text)
        reporter.post(issue, key, text)
        LinearReporter(transport, args.approver_linear_id).post(args.issue_id, key, text)
    except ReportFailed as e:
        print(f"FAIL: {e}")
        return 1
    reader = LinearDecisions(transport, args.approver_linear_id)
    me = reader.factory_id()
    mine = [c for c in reader.comments(issue) if c.id == comment_id(issue, key)]
    if len(mine) != 1 or mine[0].user_id != me:
        print(f"FAIL: expected one comment by the factory's user, found {len(mine)}")
        return 1
    if mine[0].bot is not None:
        print(f"FAIL: Linear marks the factory's comments as made by a bot ({mine[0].bot})")
        return 1
    if own_markers(mine[0], issue, me) is None:
        print("FAIL: the factory's marker lines did not survive Linear's storage unchanged")
        return 1
    print("OK: posted once under the factory's own user; retries showed nothing twice;")
    print("    its markers read back intact")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
