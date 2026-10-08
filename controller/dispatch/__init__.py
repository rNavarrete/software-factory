"""The dispatch path (ENG-176): approve if needed, then fire one attempt.

See dispatch.py. ``Launcher`` is the one fire path; the qualification script
(controller/adapter/qualify.py) uses it too.
"""

from controller.dispatch.base import BaseCheck, BaseUnreadable, GhBaseCheck
from controller.dispatch.dispatch import (
    FACTORY_ROUTINE,
    Dispatcher,
    DispatchResult,
    Fired,
    Launcher,
    Prepare,
    Refused,
    prompt_revision,
)

__all__ = [
    "FACTORY_ROUTINE",
    "BaseCheck",
    "BaseUnreadable",
    "DispatchResult",
    "Dispatcher",
    "Fired",
    "GhBaseCheck",
    "Launcher",
    "Prepare",
    "Refused",
    "prompt_revision",
]
