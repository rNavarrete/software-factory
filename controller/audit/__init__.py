"""The full control check before the pilot (ENG-163).

``python3 -m controller.audit`` checks every control in docs/governance-map.md
against the evidence that it holds: the offline tests and red-team cases that
show it refusing the unsafe case (run on the spot), and the records of what
was observed live. It prints one row per control and exits 0 only when every
control the pilot needs is observed. See ``audit.py``.
"""

from controller.audit.audit import (
    Control,
    Record,
    Report,
    Row,
    map_classes,
    run_audit,
)

__all__ = ["Control", "Record", "Report", "Row", "map_classes", "run_audit"]
