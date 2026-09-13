"""Strict canonical read failover policy.

A channel is intentionally multi-reader, but account failover is not an error
mask.  Only account/session/channel-access failures may select another reader.
Media identity failures and ordinary data/logic errors are global failures and
must surface immediately.
"""

from __future__ import annotations

import tgio
from transfer_models import ResolvedRemotePart


_ACCESS_ERROR_NAMES = {
    "AuthKeyError",
    "AuthKeyUnregisteredError",
    "ChannelInvalidError",
    "ChannelPrivateError",
    "ChatAdminRequiredError",
    "ChatForbiddenError",
    "InputUserDeactivatedError",
    "SessionExpiredError",
    "SessionPasswordNeededError",
    "SessionRevokedError",
    "UserBannedInChannelError",
    "UserDeactivatedBanError",
    "UserDeactivatedError",
}


def is_route_access_error(exc: BaseException) -> bool:
    """Whether failure is scoped to one account's ability to read the peer."""
    if isinstance(exc, PermissionError):
        return True
    return type(exc).__name__ in _ACCESS_ERROR_NAMES


_ORIGINAL_LEGACY_READ_PART = getattr(tgio._legacy, "_parity_original_read_part", None)
if _ORIGINAL_LEGACY_READ_PART is None:
    _ORIGINAL_LEGACY_READ_PART = tgio._legacy.read_part
    tgio._legacy._parity_original_read_part = _ORIGINAL_LEGACY_READ_PART


def read_part(pool, part, offset: int, length: int) -> bytes:
    if not isinstance(part, ResolvedRemotePart):
        return _ORIGINAL_LEGACY_READ_PART(pool, part, offset, length)

    last_access_error = None
    for runtime, peer in pool.read_routes(part.location):
        try:
            return runtime.worker.read_location(part.location, peer, offset, length)
        except tgio.RemoteIdentityError:
            raise
        except Exception as exc:
            if not is_route_access_error(exc):
                raise
            last_access_error = exc
    if last_access_error is not None:
        raise last_access_error
    raise tgio.ChannelRoutingError("canonical location produced no usable Telegram route")


# SeekableRemoteFile was defined in the legacy module and resolves its global
# ``read_part`` there. Patch both surfaces once so HEAD/range/full reads share
# the same strict policy.
tgio.read_part = read_part
tgio._legacy.read_part = read_part
