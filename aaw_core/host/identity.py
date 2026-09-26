"""The host's identity: a stable computer id and its relay routing token.

Persisted in ``<state dir>/host.json`` (mode 0600). The token is a routing
credential the relay binds to this computer id on first use; it is not the
encryption key (that lives in ``session.key`` and only ever reaches the phone
by QR code).
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass

from aaw_core.config import Settings


@dataclass(frozen=True)
class HostIdentity:
    computer_id: str
    token: str
    computer_name: str


def load_identity(settings: Settings) -> HostIdentity | None:
    """The persisted identity, or None if this host has never been set up."""
    try:
        d = json.loads(settings.host_file.read_text())
    except (OSError, ValueError):
        return None
    if not d.get("computer_id") or not d.get("token"):
        return None
    return HostIdentity(d["computer_id"], d["token"], d.get("computer_name") or settings.computer_name)


def load_or_create_identity(settings: Settings) -> HostIdentity:
    """The persisted identity, created on first call (the `aaw` setup path, never a hook)."""
    ident = load_identity(settings)
    if ident is not None:
        return ident
    from aaw_core.transport.relay import new_token  # the socket stack, only when minting

    # AAW_COMPUTER_ID: an installer migrating an older setup keeps the id its phones already know.
    computer_id = os.environ.get("AAW_COMPUTER_ID", "").strip() or uuid.uuid4().hex
    ident = HostIdentity(computer_id=computer_id, token=new_token(), computer_name=settings.computer_name)
    path = settings.host_file
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(asdict(ident), f, indent=2)
    return ident
