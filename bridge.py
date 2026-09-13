"""WebDAV bridge with canonical physical-location cache identity."""

from __future__ import annotations

import hashlib
import json
import sys

import _bridge_legacy as _legacy

for _name, _value in vars(_legacy).items():
    if _name not in {"__name__", "__loader__", "__package__", "__spec__"}:
        globals()[_name] = _value

from tgio import RemoteIdentityError, read_part  # noqa: E402
from transfer_models import physical_location_key  # noqa: E402


def _physical_set_key(parts) -> str:
    """Stable disk/ZIP identity for the ordered physical byte set."""
    payload = json.dumps(
        [physical_location_key(part.location) for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return "loc3-" + hashlib.sha256(payload).hexdigest()


def _fresh_parts(self, entry):
    return tuple(self.api.current_parts(entry))


def _cache_key(self, entry):
    return _physical_set_key(self._fresh_parts(entry))


def _head_path_for(self, parts):
    return self.cfg.cache_dir / "heads" / f"{_physical_set_key(parts)}{HEAD_SUFFIX}"


def _thumb_path_for(self, parts):
    return self.cfg.cache_dir / "thumbs" / f"{_physical_set_key(parts)}{THUMB_SUFFIX}"


def _open_remote(self, entry):
    """Refresh canonical physical rows immediately before opening bytes."""
    self.note_demand()
    parts = self._fresh_parts(entry)
    try:
        head = _head_path_for(self, parts).read_bytes()
    except OSError:
        head = b""
    return SeekableRemoteFile(self.pool, parts, name=entry.name, head=head)


def _thumb_path(self, entry):
    return _thumb_path_for(self, self._fresh_parts(entry))


def _head_path(self, entry):
    return _head_path_for(self, self._fresh_parts(entry))


def _read_via_routes(self, part, operation):
    last_error = None
    for runtime, peer in self.pool.read_routes(part.location):
        try:
            return operation(runtime.worker, part.location, peer)
        except RemoteIdentityError:
            raise
        except Exception as exc:
            last_error = exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("canonical location produced no Telegram route")


def _thumbs_for(self, entries):
    """Refresh physical identity before cache lookup and thumbnail RPC."""
    found = {}
    for entry in entries:
        if entry.is_dir or not entry.has_thumbnail:
            continue
        parts = self._fresh_parts(entry)
        if not parts:
            continue
        path = _thumb_path_for(self, parts)
        try:
            data = path.read_bytes()
        except OSError:
            data = None
        if data is None:
            self.note_demand()
            part = parts[0]
            data = _read_via_routes(
                self,
                part,
                lambda worker, location, peer: worker.thumbnail_location(location, peer),
            )
            if data:
                try:
                    _write_atomic(path, data)
                except OSError as exc:
                    log.warning("could not cache thumbnail %s: %s", path.name, exc)
        if data:
            # Preserve the public/result shape expected by the shell RPC.
            found[(entry.telegram_user_id, entry.file_id)] = data
    return found


def _props_for(self, entries, *, demand=True):
    """Media-property cache is physical, not a logical file-id cache."""
    found = {}
    if demand:
        self.note_demand()
    changed = False
    for entry in entries:
        if entry.is_dir:
            continue
        parts = self._fresh_parts(entry)
        if not parts:
            continue
        key = _physical_set_key(parts)
        info = self._prop_cache.get(key)
        if info is None:
            part = parts[0]
            info = _read_via_routes(
                self,
                part,
                lambda worker, location, peer: worker.media_info_location(location, peer),
            )
            self._prop_cache.put(key, info, defer=True)
            changed = True
        found[(entry.telegram_user_id, entry.file_id)] = info
    if changed:
        self._prop_cache.flush()
    return found


def _cached_head(self, entry):
    parts = self._fresh_parts(entry)
    try:
        return _head_path_for(self, parts).read_bytes()
    except OSError:
        return b""


def _head_complete(self, entry):
    parts = self._fresh_parts(entry)
    try:
        have = _head_path_for(self, parts).stat().st_size
    except OSError:
        return False
    expected = sum(part.size for part in parts)
    return have >= min(HEAD_SIZE, expected)


def _heads_for(self, entries, *, before=None):
    missing = [entry for entry in entries if self.wants_head(entry) and not self._head_complete(entry)]
    if not missing:
        return 0

    def one(entry):
        try:
            parts = self._fresh_parts(entry)
            if not parts:
                return False
            data = read_part(self.pool, parts[0], 0, HEAD_SIZE)
            if not data:
                return False
            _write_atomic(_head_path_for(self, parts), data)
            return True
        except Exception as exc:
            log.warning("could not cache head of %s: %s", entry.name, exc)
            return False

    done = 0
    for at in range(0, len(missing), HEAD_BATCH):
        if before is not None and before() is False:
            break
        group = missing[at : at + HEAD_BATCH]
        with ThreadPoolExecutor(max_workers=HEAD_BATCH) as executor:
            done += sum(1 for ok in executor.map(one, group) if ok)
    return done


def _needs_warming(self, entry):
    if entry.is_dir:
        return False
    parts = self._fresh_parts(entry)
    if not parts:
        return False
    if entry.has_thumbnail and not _thumb_path_for(self, parts).exists():
        return True
    return self._prop_cache.get(_physical_set_key(parts)) is None


Resolver.fresh_parts = _fresh_parts
Resolver._cache_key = _cache_key
Resolver.open_remote = _open_remote
Resolver._thumb_path = _thumb_path
Resolver._head_path = _head_path
Resolver.cached_head = _cached_head
Resolver._head_complete = _head_complete
Resolver.thumbs_for = _thumbs_for
Resolver.props_for = _props_for
Resolver.heads_for = _heads_for
Resolver.needs_warming = _needs_warming

# Expose for focused cache tests without teaching callers about hash encoding.
physical_set_cache_key = _physical_set_key


if __name__ == "__main__":
    sys.exit(main())
