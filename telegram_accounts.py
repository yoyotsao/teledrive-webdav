"""Account-pool parity extensions for canonical Saved Messages/channel routing."""

from __future__ import annotations

import _telegram_accounts_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from dataclasses import dataclass, field  # noqa: E402
from transfer_models import FileLocation, LegacySavedMessagesLocation  # noqa: E402


@dataclass(frozen=True)
class ChannelAccess:
    """Account/session-local Telegram channel peer and current capabilities."""

    channel_id: str
    peer: object = field(repr=False, compare=False)
    can_read: bool = True
    can_write: bool = False
    session_generation: int = 0


def _read_routes(self, location):
    """Return viable account-local routes for one canonical physical location."""
    if isinstance(location, LegacySavedMessagesLocation):
        runtime = self.for_read(location.telegram_user_id)
        return ((runtime, "me"),)

    if not isinstance(location, FileLocation):
        raise TypeError(f"unsupported location type: {type(location).__name__}")

    if location.telegram_chat_id is None:
        if location.telegram_user_id is None:
            raise AccountUnavailableError("canonical Saved Messages location has no storage account")
        runtime = self.for_read(location.telegram_user_id)
        return ((runtime, "me"),)

    routes = []
    errors = []
    for runtime in self._runtimes:
        if not (runtime.online and runtime.linked):
            continue
        try:
            access = runtime.worker.resolve_channel_access(location.telegram_chat_id)
        except Exception as exc:
            errors.append(f"{runtime.telegram_user_id}: {type(exc).__name__}: {exc}")
            continue
        if access.can_read:
            routes.append((runtime, access.peer))
    if not routes:
        detail = "; ".join(errors) if errors else "no online linked account can access channel"
        raise AccountUnavailableError(
            f"no Telegram reader for channel {location.telegram_chat_id}: {detail}"
        )
    return tuple(routes)


def _channel_writers(self, channel_id: str, linked_ids):
    allowed = {int(item) for item in linked_ids}
    writers = []
    errors = []
    for runtime in self._runtimes:
        user_id = int(runtime.telegram_user_id)
        if user_id not in allowed or not (runtime.online and runtime.linked):
            continue
        try:
            access = runtime.worker.resolve_channel_access(str(channel_id))
        except Exception as exc:
            errors.append(f"{user_id}: {type(exc).__name__}: {exc}")
            continue
        if access.can_write:
            writers.append((runtime, access))
    if not writers:
        detail = "; ".join(errors) if errors else "no current writer permission"
        raise AccountUnavailableError(f"no Telegram writer for channel {channel_id}: {detail}")
    return tuple(writers)


def _primary_for_write(self, expected_user_id: int):
    primary = self.primary
    if not (primary.online and primary.linked):
        raise AccountUnavailableError(self._unavailable_message(primary))
    actual = int(primary.telegram_user_id)
    if int(expected_user_id) != actual:
        raise AccountUnavailableError(
            f"frozen primary account {expected_user_id} does not match local primary {actual}"
        )
    return primary


TelegramAccountPool.read_routes = _read_routes
TelegramAccountPool.channel_writers = _channel_writers
TelegramAccountPool.primary_for_write = _primary_for_write
