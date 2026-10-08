"""Bounded automatic repairs (ENG-160).

When the independent review (ENG-156) or CI finds a routine problem in a
worker's PR, the factory may start one more attempt of the same task to fix
it, without Rolando typing anything, but only:

- within the repair allowance the project's onboarding entry gave the Todo
  move that started the task (``repair_allowance``, 0 unless set), and within
  the contract's attempt budget and every existing fire limit;
- for findings that are routine technical fixes (``policy.plan``); anything
  touching a protected control, the product, security, or a request to
  weaken a check goes to Rolando instead;
- once the earlier worker is known to have stopped (a clearing record), on
  the exact commit the findings were raised on;
- with a go-ahead the signer process signs after checking with Linear that
  the Todo move still stands (``source-repair-authorized``).

``findings.py`` holds the finding shape and its digest, ``policy.py`` the
decision, ``review.py`` the bridge from the review's verdict. Nothing here
does I/O or signs anything.
"""
