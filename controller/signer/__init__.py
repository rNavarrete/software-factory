"""The signer: the only process on the service host that holds the approval key.

See ``signer.py``.
"""

from controller.signer.signer import (
    SignerKey,
    SignerServer,
    SignerUnavailable,
    drop_privileges,
)

__all__ = ["SignerKey", "SignerServer", "SignerUnavailable", "drop_privileges"]
