"""``python3 -m controller.intake probe <ISSUE-KEY>``: read-only check of how
Linear records who changed a ticket.

Prints each history entry of the ticket: when, the state change, who Linear
says made it, and whether it carries a bot, an automation or an import. Run
it once on the host before intake is switched on, on a ticket a Claude
session (the Linear connector) has changed, to see that such changes carry a
bot and so never count as Rolando's. It writes nothing anywhere.
"""

from __future__ import annotations

import os
import sys

from controller.intake.linear import HttpTransport, LinearSource, attribution_problem
from controller.service.secrets import FileSecrets


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 3 or args[0] != "probe":
        print("usage: python3 -m controller.intake probe <ISSUE-KEY> <ROLANDO-LINEAR-USER-ID>")
        return 2
    secrets = FileSecrets(os.environ.get("FACTORY_SECRETS_DIR", ""))
    transport = HttpTransport(lambda: secrets.get("linear-key"))
    source = LinearSource(transport, policy=lambda: None)  # type: ignore[arg-type,return-value]
    viewer = transport("query { viewer { id name app } }", {})["viewer"]
    print(f"the factory's Linear key acts as: {viewer.get('name')} ({viewer.get('id')})")
    if viewer.get("id") == args[2]:
        print("  PROBLEM: that is Rolando. The factory needs its own Linear identity.")
    ticket = source.fetch(args[1])
    if ticket is None:
        print(f"{args[1]}: not found")
        return 1
    print(f"{ticket.key}: now in {ticket.state.name}")
    for c in ticket.changes:
        move = (
            f"{c.from_state.name if c.from_state else '-'} -> {c.to_state.name}"
            if c.to_state
            else ", ".join(c.content) or "other"
        )
        verdict = attribution_problem(c, args[2]) or "counts as Rolando's own action"
        print(
            f"  {c.at:%Y-%m-%d %H:%M:%S} {move:28} actor={c.actor_name!r}"
            f" bot={c.bot!r} automation={c.automation} import={c.imported}: {verdict}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
