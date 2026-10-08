"""What the factory says on Linear tickets, and what it reads back (ENG-178).

- ``messages``: the plain-English texts (progress, questions, readiness,
  merge and release records, time). Pure functions.
- ``reporter``: ``LinearReporter``, the service's ``Reporter``. Posts each
  message once, whatever happens between a post and its record.
- ``replies``: ``LinearDecisions``, Rolando's answers, observations and time
  entries, read with the same care as a Todo move.
- ``linear_api``: the GraphQL transport they share.
"""
