"""Telegram transport parity extensions over the proven byte transport."""

from __future__ import annotations

import mimetypes
import secrets
import _tgio_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from transfer_models import (  # noqa: E402
    DurableTelegramWrite,
    FileLocation,
    LegacySavedMessagesLocation,
    ResolvedRemotePart,
    physical_location_key,
)


class ChannelRoutingError(RuntimeError):
    pass


def make_preview(path, mime_type: str = "", ffmpeg=None):
    """Keep the historical preview API monkeypatchable from ``tgio``.

    The legacy implementation lives in another module now, so referring to its
    module-global ``capture_thumbnail`` would bypass existing tests and callers
    that intentionally replace ``tgio.capture_thumbnail``. Resolve the seam in
    this compatibility module instead.
    """
    mime = mime_type or mimetypes.guess_type(str(path))[0] or ""
    result = capture_thumbnail(path, mime, ffmpeg)
    if result.kind != "ready":
        if result.error:
            log.info("no preview for %s: %s", getattr(path, "name", path), result.error)
        return None
    return result.jpeg, result.width, result.height


def _media_kind(media) -> str:
    return "photo" if _legacy._is_photo(media) else "document"


def _canonical_size(media) -> int:
    helper = getattr(_legacy, "_media_size", None)
    if callable(helper):
        return int(helper(media) or 0)
    return int(getattr(media, "size", 0) or 0)


def _photo_variant(media):
    sizes = list(getattr(media, "sizes", None) or [])
    if not sizes:
        return None

    def score(item):
        explicit = getattr(item, "size", None)
        if explicit is not None:
            try:
                return int(explicit)
            except (TypeError, ValueError):
                pass
        return int(getattr(item, "w", 0) or 0) * int(getattr(item, "h", 0) or 0)

    best = max(sizes, key=score)
    variant = getattr(best, "type", None)
    return str(variant) if variant not in (None, "") else None


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


# Captured before the seam below rebinds ``_legacy.read_part`` to the wrapper
# defined here. Reaching for ``_legacy.read_part`` at call time would find that
# rebinding and recurse until the stack runs out -- and it is the *legacy* part
# shape that takes this branch, so every pre-canonical row on the drive would
# take it.
_legacy_read_part = _legacy.read_part


def read_part(pool, part, offset: int, length: int) -> bytes:
    """Read a legacy or canonical part, failing over only account-local errors."""
    if not isinstance(part, ResolvedRemotePart):
        return _legacy_read_part(pool, part, offset, length)
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


# -- deterministic message-producing RPCs --------------------------------- #

_MIN_RANDOM_ID = -(1 << 63)
_MAX_RANDOM_ID = (1 << 63) - 1


def normalise_random_id(value) -> int:
    """Return a Telegram signed int64 random id without lossy wrapping."""
    if isinstance(value, bool):
        raise ValueError("random_id must be a signed int64")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("random_id must be a signed int64") from exc
    if result < _MIN_RANDOM_ID or result > _MAX_RANDOM_ID:
        raise ValueError("random_id must be a signed int64")
    return result


def generate_random_id() -> int:
    raw = secrets.randbits(64)
    return raw - (1 << 64) if raw >= (1 << 63) else raw


def _message_candidates(response):
    mapped = {}
    messages = {}
    for update in list(getattr(response, "updates", None) or []):
        random_id = getattr(update, "random_id", None)
        message_id = getattr(update, "id", None)
        if random_id is not None and message_id is not None:
            try:
                mapped[normalise_random_id(random_id)] = int(message_id)
            except (TypeError, ValueError):
                pass
        message = getattr(update, "message", None)
        if message is not None and getattr(message, "id", None) is not None:
            messages[int(message.id)] = message
    return mapped, messages


async def _message_for_random(client, response, target_peer, random_id: int):
    mapped, messages = _message_candidates(response)
    destination = mapped.get(random_id)
    if destination is not None:
        message = messages.get(destination)
        if message is None:
            fetched = await client.get_messages(target_peer, ids=[destination])
            message = fetched[0] if fetched else None
        if message is None:
            raise RemoteIdentityError(
                f"Telegram mapped random_id {random_id} to missing message {destination}"
            )
        return message
    if len(messages) == 1:
        # Compatibility for transports that omit UpdateMessageID. Durable
        # recovery never relies on this fallback: it keys by uploader/random_id.
        return next(iter(messages.values()))
    raise RemoteIdentityError(f"Telegram returned no unique mapping for random_id {random_id}")


def _durable_write(self, message, *, random_id: int, target_peer_key: str, expected_size=None):
    media = _legacy._message_media(message)
    if media is None:
        raise RemoteIdentityError("Telegram accepted the write but returned no media")
    media_size = _canonical_size(media)
    if not media_size and expected_size is not None:
        media_size = int(expected_size)
    return DurableTelegramWrite(
        uploader_id=int(self.user_id),
        random_id=normalise_random_id(random_id),
        target_peer_key=str(target_peer_key),
        destination_message_id=int(message.id),
        media_kind=_media_kind(media),
        media_id=str(media.id),
        media_size=media_size,
        access_hash=(str(media.access_hash) if getattr(media, "access_hash", None) is not None else None),
        photo_variant=_photo_variant(media) if _media_kind(media) == "photo" else None,
    )


async def _target_rpc(self, request, message_limiter):
    client = await self._upload_client()
    for attempt in range(3):
        if message_limiter is not None:
            await message_limiter.acquire()
        try:
            return client, await client(request)
        except Exception as exc:
            wait = _legacy._flood_seconds(exc)
            if wait is None or message_limiter is None:
                raise
            message_limiter.flood(wait)
            if attempt == 2:
                raise


def _send_uploaded_segment_to(
    self, handle, size, file_name, *, target_peer, target_peer_key, random_id,
    preview=None, mime_type=None, message_limiter=None,
):
    return self.run(
        self._send_uploaded_segment_to(
            handle, size, file_name, target_peer=target_peer, target_peer_key=target_peer_key,
            random_id=random_id, preview=preview, mime_type=mime_type,
            message_limiter=message_limiter,
        ),
        timeout=None,
    )


async def _send_uploaded_segment_to_async(
    self, handle, size, file_name, *, target_peer, target_peer_key, random_id,
    preview=None, mime_type=None, message_limiter=None,
):
    from telethon.tl.functions.messages import SendMediaRequest
    from telethon.tl.types import (
        DocumentAttributeFilename, DocumentAttributeImageSize, InputMediaUploadedDocument,
    )

    random_id = normalise_random_id(random_id)
    attributes = [DocumentAttributeFilename(file_name)]
    thumb = None
    if preview is not None:
        thumb, width, height = preview
        attributes.append(DocumentAttributeImageSize(width, height))
    media = InputMediaUploadedDocument(
        file=handle,
        mime_type=mime_type or mimetypes.guess_type(file_name)[0] or "application/octet-stream",
        attributes=attributes,
        thumb=thumb,
    )
    request = SendMediaRequest(
        peer=target_peer, media=media, message="", random_id=random_id,
    )
    client, response = await _target_rpc(self, request, message_limiter)
    message = await _message_for_random(client, response, target_peer, random_id)
    return _durable_write(
        self, message, random_id=random_id, target_peer_key=target_peer_key, expected_size=size,
    )


def _send_album_to(
    self, items, *, target_peer, target_peer_key, random_ids, timeout=60, message_limiter=None,
):
    return self.run(
        self._send_album_to(
            items, target_peer=target_peer, target_peer_key=target_peer_key,
            random_ids=random_ids, timeout=timeout, message_limiter=message_limiter,
        ),
        timeout=None,
    )


async def _send_album_to_async(
    self, items, *, target_peer, target_peer_key, random_ids, timeout=60, message_limiter=None,
):
    from telethon.tl.functions.messages import SendMultiMediaRequest
    from telethon.tl.types import InputDocument, InputMediaDocument, InputSingleMedia

    if not 1 <= len(items) <= 10:
        raise ValueError("an album must contain between one and ten items")
    if any(item.telegram_user_id != int(self.user_id) for item in items):
        raise ValueError("an album cannot mix Telegram accounts")
    ids = tuple(normalise_random_id(value) for value in random_ids)
    if len(ids) != len(items) or len(set(ids)) != len(ids):
        raise ValueError("album requires one unique durable random_id per child")
    request = SendMultiMediaRequest(
        peer=target_peer,
        multi_media=[
            InputSingleMedia(
                media=InputMediaDocument(InputDocument(
                    int(item.document_id), int(item.access_hash), item.file_reference,
                )),
                random_id=random_id,
                message="",
            )
            for item, random_id in zip(items, ids)
        ],
    )
    client = await self._upload_client()
    response = await asyncio.wait_for(
        _target_rpc(self, request, message_limiter), timeout=timeout,
    )
    _, response = response
    mapped, messages = _message_candidates(response)
    by_document = {}
    for message in messages.values():
        media = _legacy._message_media(message)
        if media is not None:
            by_document[str(media.id)] = message
    writes = []
    for item, random_id in zip(items, ids):
        destination = mapped.get(random_id)
        message = messages.get(destination) if destination is not None else None
        if message is None and destination is not None:
            fetched = await client.get_messages(target_peer, ids=[destination])
            message = fetched[0] if fetched else None
        if message is None:
            message = by_document.get(str(item.document_id))
        if message is None:
            raise RemoteIdentityError(
                f"album returned no exact result for random_id {random_id}"
            )
        writes.append(_durable_write(
            self, message, random_id=random_id, target_peer_key=target_peer_key,
            expected_size=item.size,
        ))
    return tuple(writes)


def _forward_messages_to(
    self, from_peer, message_ids, *, target_peer, target_peer_key, random_ids,
    message_limiter=None,
):
    return self.run(
        self._forward_messages_to(
            from_peer, message_ids, target_peer=target_peer, target_peer_key=target_peer_key,
            random_ids=random_ids, message_limiter=message_limiter,
        ),
        timeout=None,
    )


async def _forward_messages_to_async(
    self, from_peer, message_ids, *, target_peer, target_peer_key, random_ids,
    message_limiter=None,
):
    from telethon.tl.functions.messages import ForwardMessagesRequest

    message_ids = tuple(int(value) for value in message_ids)
    ids = tuple(normalise_random_id(value) for value in random_ids)
    if not message_ids or len(message_ids) != len(ids) or len(set(ids)) != len(ids):
        raise ValueError("forward requires one unique durable random_id per message")
    request = ForwardMessagesRequest(
        from_peer=from_peer, id=list(message_ids), random_id=list(ids), to_peer=target_peer,
    )
    client, response = await _target_rpc(self, request, message_limiter)
    writes = []
    for random_id in ids:
        message = await _message_for_random(client, response, target_peer, random_id)
        writes.append(_durable_write(
            self, message, random_id=random_id, target_peer_key=target_peer_key,
        ))
    return tuple(writes)


TelegramWorker.start = _parity_start
TelegramWorker.stop = _parity_stop
TelegramWorker.resolve_channel_access = _resolve_channel_access
TelegramWorker.get_location_media = _get_location_media
TelegramWorker.read_location = _read_location
TelegramWorker.thumbnail_location = _thumbnail_location
TelegramWorker.media_info_location = _media_info_location
TelegramWorker.send_uploaded_segment_to = _send_uploaded_segment_to
TelegramWorker._send_uploaded_segment_to = _send_uploaded_segment_to_async
TelegramWorker.send_album_to = _send_album_to
TelegramWorker._send_album_to = _send_album_to_async
TelegramWorker.forward_messages_to = _forward_messages_to
TelegramWorker._forward_messages_to = _forward_messages_to_async

# Make the signed-int64 contract available beside the upload primitives without
# duplicating Telegram ID rules across modules.
tgupload.normalise_random_id = normalise_random_id
tgupload.generate_random_id = generate_random_id

# Classes/functions copied from the original module resolve names in the legacy
# module globals. Keep the deliberately replaceable seams synchronized.
#
# Anything here that the wrapper itself falls back to must be captured above
# before this runs -- see ``_legacy_read_part``.
_legacy.read_part = read_part
_legacy.make_preview = make_preview
