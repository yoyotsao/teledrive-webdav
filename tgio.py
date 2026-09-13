"""Telegram transport parity extensions over the proven byte transport."""

from __future__ import annotations

import _tgio_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from transfer_models import (  # noqa: E402
    FileLocation,
    LegacySavedMessagesLocation,
    ResolvedRemotePart,
    physical_location_key,
)


class ChannelRoutingError(RuntimeError):
    pass


def _media_kind(media) -> str:
    return "photo" if _legacy._is_photo(media) else "document"


def _canonical_size(media) -> int:
    helper = getattr(_legacy, "_media_size", None)
    if callable(helper):
        return int(helper(media) or 0)
    return int(getattr(media, "size", 0) or 0)


def validate_canonical_media(media, location: FileLocation) -> None:
    """Validate immutable backend media identity before exposing any bytes."""
    actual_kind = _media_kind(media)
    if actual_kind != location.media_kind:
        raise RemoteIdentityError(
            f"Telegram media kind mismatch: expected {location.media_kind}, got {actual_kind}"
        )
    if str(getattr(media, "id", "")) != str(location.media_id):
        raise RemoteIdentityError(
            f"Telegram media id mismatch: expected {location.media_id}, got {getattr(media, 'id', None)}"
        )
    actual_size = _canonical_size(media)
    if actual_size != int(location.media_size):
        raise RemoteIdentityError(
            f"Telegram media size mismatch: expected {location.media_size}, got {actual_size}"
        )
    if location.media_kind == "photo" and location.photo_variant:
        variants = {
            str(getattr(item, "type", ""))
            for item in (getattr(media, "sizes", None) or [])
            if getattr(item, "type", None) is not None
        }
        if location.photo_variant not in variants:
            raise RemoteIdentityError(
                f"Telegram photo variant mismatch: expected {location.photo_variant}"
            )


def _session_generation(self) -> int:
    return int(getattr(self, "_parity_session_generation", 0))


_original_start = TelegramWorker.start
_original_stop = TelegramWorker.stop


def _parity_start(self):
    before = getattr(self, "_thread", None)
    result = _original_start(self)
    if before is None and getattr(self, "_thread", None) is not None:
        self._parity_session_generation = _session_generation(self) + 1
        self._parity_channel_cache = {}
        self._parity_location_cache = {}
    return result


def _parity_stop(self):
    try:
        return _original_stop(self)
    finally:
        self._parity_session_generation = _session_generation(self) + 1
        self._parity_channel_cache = {}
        self._parity_location_cache = {}


async def _resolve_channel_access_async(self, channel_id: str):
    from telegram_accounts import ChannelAccess

    raw = str(channel_id)
    try:
        lookup = int(raw)
    except ValueError:
        lookup = raw
    entity = await self._client.get_entity(lookup)
    peer = await self._client.get_input_entity(entity)
    can_read = True
    can_write = False
    try:
        permissions = await self._client.get_permissions(entity, "me")
    except Exception:
        permissions = None
    if permissions is not None:
        can_write = bool(
            getattr(permissions, "is_creator", False)
            or getattr(permissions, "is_admin", False)
        )
    if not can_write and not bool(getattr(entity, "broadcast", False)):
        can_write = True
    return ChannelAccess(
        channel_id=raw,
        peer=peer,
        can_read=can_read,
        can_write=can_write,
        session_generation=_session_generation(self),
    )


def _resolve_channel_access(self, channel_id: str):
    generation = _session_generation(self)
    cache = getattr(self, "_parity_channel_cache", None)
    if cache is None:
        cache = self._parity_channel_cache = {}
    key = (generation, str(channel_id))
    cached = cache.get(key)
    if cached is not None:
        return cached
    access = self.run(_resolve_channel_access_async(self, str(channel_id)), timeout=60)
    cache[key] = access
    return access


async def _fetch_location_media(self, location: FileLocation, peer):
    messages = await self._client.get_messages(peer, ids=[location.telegram_message_id])
    msg = messages[0] if messages else None
    media = _legacy._message_media(msg)
    if media is None:
        raise FileNotFoundError(
            f"Telegram message {location.telegram_message_id} has no document or photo"
        )
    validate_canonical_media(media, location)
    return media


def _get_location_media(self, location: FileLocation, peer, refresh: bool = False):
    cache = getattr(self, "_parity_location_cache", None)
    if cache is None:
        cache = self._parity_location_cache = {}
    key = physical_location_key(location)
    now = time.monotonic()
    hit = cache.get(key)
    if not refresh and hit and now - hit[1] < DOC_CACHE_TTL:
        return hit[0]
    media = self.run(_fetch_location_media(self, location, peer), timeout=60)
    cache[key] = (media, now)
    return media


def _read_location(self, location, peer, offset: int, length: int) -> bytes:
    if isinstance(location, LegacySavedMessagesLocation):
        return self.read(location.telegram_message_id, location.file_id, offset, length)
    if length <= 0:
        return b""
    media = self.get_location_media(location, peer)
    try:
        return self.run(self._read(media, offset, length))
    except Exception as exc:
        if not _legacy._is_file_reference_error(exc):
            raise
        media = self.get_location_media(location, peer, refresh=True)
        return self.run(self._read(media, offset, length))


def _thumbnail_location(self, location, peer):
    if isinstance(location, LegacySavedMessagesLocation):
        part = RemotePart(
            location.telegram_message_id,
            location.media_size,
            location.telegram_user_id,
            location.file_id,
        )
        return self.thumbnails([part]).get((part.message_id, str(part.file_id)))
    media = self.get_location_media(location, peer)
    return self.run(self._thumbnail_bytes(media), timeout=60)


def _media_info_location(self, location, peer):
    if isinstance(location, LegacySavedMessagesLocation):
        media = self.get_document(location.telegram_message_id, location.file_id)
    else:
        media = self.get_location_media(location, peer)
    return _legacy._media_attributes(media)


def read_part(pool, part, offset: int, length: int) -> bytes:
    """Read a legacy or canonical part, failing over only account-local errors."""
    if not isinstance(part, ResolvedRemotePart):
        return _legacy.read_part(pool, part, offset, length)
    last_error = None
    for runtime, peer in pool.read_routes(part.location):
        try:
            return runtime.worker.read_location(part.location, peer, offset, length)
        except RemoteIdentityError:
            raise
        except Exception as exc:
            last_error = exc
            continue
    raise ChannelRoutingError(
        f"no usable Telegram route for {physical_location_key(part.location)!r}: {last_error}"
    )


TelegramWorker.start = _parity_start
TelegramWorker.stop = _parity_stop
TelegramWorker.resolve_channel_access = _resolve_channel_access
TelegramWorker.get_location_media = _get_location_media
TelegramWorker.read_location = _read_location
TelegramWorker.thumbnail_location = _thumbnail_location
TelegramWorker.media_info_location = _media_info_location

# SeekableRemoteFile is defined in the legacy module and resolves read_part in
# that module's globals, so update that single seam rather than copying its
# streaming/block-cache implementation.
_legacy.read_part = read_part
