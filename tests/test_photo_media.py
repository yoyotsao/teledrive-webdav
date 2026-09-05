"""Files whose Telegram message is a *photo*, not a document.

The backend's chat-media import registers messages straight out of chats, and
those arrive as MessageMediaPhoto. Everything in tgio was written against
Document -- `.size`, `.thumbs`, `.attributes` -- so a photo-backed entry had no
preview, no dimensions, and could not be read at all: the bridge answered 500
and the shell handler, unable to tell that from "no thumbnail exists",
delegated to the built-in provider, which tried to read the whole file and
failed too. One folder of 3,756 files had 2,066 of them, and a warm-up pass
across it dropped every pooled Telegram connection.

A photo carries the same three identity fields as a document and a `sizes` list
instead of `thumbs`. The full file is `sizes[-1]`, and its byte count is what
the backend recorded as the file size -- verified against four live messages,
exact match on all four.
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import tgio  # noqa: E402


class _Stripped:
    """~100 bytes of blur that is not a JPEG until a header is bolted on."""
    type = "i"


class _PhotoSize:
    def __init__(self, type_, w, h, size):
        self.type, self.w, self.h, self.size = type_, w, h, size


class _Progressive:
    """The full image, delivered as progressive scans. Its byte count is the
    last entry of `sizes`, not their sum."""
    def __init__(self, type_, w, h, sizes):
        self.type, self.w, self.h, self.sizes = type_, w, h, sizes


class _Photo:
    def __init__(self, sizes=None, dc_id=1):
        self.id = 555
        self.access_hash = 777
        self.file_reference = b"pref"
        self.dc_id = dc_id
        self.sizes = sizes if sizes is not None else [
            _Stripped(),
            _PhotoSize("m", 240, 320, 32_092),
            _PhotoSize("x", 600, 800, 150_230),
            _Progressive("y", 960, 1280, [20_969, 70_553, 127_063, 187_027, 283_437]),
        ]


class _Doc:
    def __init__(self):
        self.id = 1
        self.access_hash = 2
        self.file_reference = b"d"
        self.dc_id = 1
        self.size = 4_000_000
        self.mime_type = "video/mp4"
        self.thumbs = [_PhotoSize("m", 90, 160, 17_000)]
        self.attributes = []


class _Msg:
    def __init__(self, id_, document=None, photo=None):
        self.id, self.document, self.photo = id_, document, photo


# --------------------------------------------------------------------------- #
# size
# --------------------------------------------------------------------------- #


def test_photo_size_is_the_last_entrys_byte_count():
    """283437 is the last element, not the sum -- progressive `sizes` are
    cumulative scan lengths. Matched exactly against what the backend recorded
    for four live messages."""
    assert tgio._media_size(_Photo()) == 283_437


def test_a_photo_whose_full_size_is_a_plain_PhotoSize():
    photo = _Photo(sizes=[_Stripped(), _PhotoSize("m", 320, 266, 17_941),
                          _PhotoSize("x", 510, 424, 37_317)])
    assert tgio._media_size(photo) == 37_317


def test_document_size_is_unchanged():
    assert tgio._media_size(_Doc()) == 4_000_000


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #


class _Client:
    def __init__(self, messages):
        self._messages = messages
        self.downloads = []

    async def get_messages(self, entity, ids=None):
        return [self._messages.get(i) for i in ids]

    def iter_download(self, file, **kwargs):
        self.downloads.append((file, kwargs))

        async def gen():
            yield b"\xff\xd8photo"
        return gen()


def worker_with(messages):
    w = tgio.TelegramWorker.__new__(tgio.TelegramWorker)
    w._client = _Client(messages)
    w._docs = {}
    import threading
    w._docs_lock = threading.Lock()
    return w


def test_a_photo_message_resolves_instead_of_raising():
    """Before: FileNotFoundError("has no document") -> 500 on every read."""
    photo = _Photo()
    w = worker_with({9: _Msg(9, photo=photo)})
    assert asyncio.run(w._fetch_document(9)) is photo


def test_a_message_with_neither_still_raises():
    w = worker_with({9: _Msg(9)})
    with pytest.raises(FileNotFoundError):
        asyncio.run(w._fetch_document(9))


def test_a_missing_message_still_raises():
    w = worker_with({})
    with pytest.raises(FileNotFoundError):
        asyncio.run(w._fetch_document(9))


# --------------------------------------------------------------------------- #
# thumbnail
# --------------------------------------------------------------------------- #


def test_photo_preview_picks_a_small_complete_size():
    """Not sizes[-1] -- that is the full image, and downloading it per file is
    exactly the whole-file read the preview exists to avoid. Not the stripped
    one either: it is not a JPEG on its own. And not "x" at 150 KB of a 283 KB
    file: a preview that costs half the original has bought nothing."""
    thumb = tgio._best_thumb(_Photo())
    assert thumb.type == "m"
    assert thumb.size <= tgio.THUMB_PREVIEW_MAX


def test_a_photo_whose_every_preview_is_over_the_cap_still_answers():
    """Better the smallest oversized preview than a 404, which sends the shell
    to the built-in handler to read the original in full."""
    photo = _Photo(sizes=[_Stripped(),
                          _PhotoSize("x", 600, 800, 150_230),
                          _PhotoSize("z", 800, 1000, 200_000),
                          _Progressive("y", 960, 1280, [1, 2, 283_437])])
    assert tgio._best_thumb(photo).type == "x"


def test_photo_preview_falls_back_when_only_the_full_size_is_concrete():
    """A photo small enough to have no intermediate size still needs an answer;
    at that point the full image *is* the cheap one."""
    photo = _Photo(sizes=[_Stripped(), _PhotoSize("m", 320, 266, 17_941)])
    assert tgio._best_thumb(photo).type == "m"


def test_document_preview_still_comes_from_thumbs():
    assert tgio._best_thumb(_Doc()).type == "m"


def test_photo_preview_uses_an_InputPhotoFileLocation():
    """A photo addressed with InputDocumentFileLocation is answered with
    LOCATION_INVALID -- the two are different constructors on the wire."""
    from telethon.tl.types import InputPhotoFileLocation

    photo = _Photo()
    w = worker_with({})
    client = _Client({})
    w._pool = [client]
    w._rr = 0

    async def pool():
        return [client]

    w._download_pool = pool
    data = asyncio.run(w._thumbnail_bytes(photo))
    assert data == b"\xff\xd8photo"
    location, kwargs = client.downloads[0]
    assert isinstance(location, InputPhotoFileLocation)
    assert location.id == 555 and location.thumb_size == "m"
    # dc_id must be passed or a cross-DC photo comes back FILE_MIGRATE, which
    # Telethon only follows inside iter_download.
    assert kwargs["dc_id"] == 1


# --------------------------------------------------------------------------- #
# properties
# --------------------------------------------------------------------------- #


def test_photo_dimensions_come_from_the_full_size():
    """The shell reads a JPEG's header purely to get w/h. Answering from the
    photo means it never opens the file."""
    assert tgio._media_attributes(_Photo()) == {
        "width": 960, "height": 1280, "mime": "image/jpeg",
    }


def test_document_attributes_are_unchanged():
    assert tgio._media_attributes(_Doc()) == {"mime": "video/mp4"}
