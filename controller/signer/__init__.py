"""The signer: the only process on the service host that holds the approval key.

See ``signer.py``.
"""

from controller.signer.signer import (
    SignerAuthorizer,
    SignerKey,
    SignerServer,
    SignerUnavailable,
    authorize_handler,
    drop_privileges,
)

__all__ = [
    "SignerAuthorizer",
    "SignerKey",
    "SignerServer",
    "SignerUnavailable",
    "authorize_handler",
    "drop_privileges",
]
