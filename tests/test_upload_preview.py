"""What this bridge attaches to its own uploads, so H: can draw them.

Offline. Telegram is never contacted: the assertions are about the preview
bytes and the attributes handed to ``send_file``.

An upload used to reach Telegram as a bare document with only a filename
attribute, so ``doc.thumbs`` came back empty and there were no dimensions.
Every layer downstream then failed in the one way this project cannot afford:
/rpc/thumb answered 404, the shell handler could not tell that from "fetch
failed", and the built-in handler it delegated to read the whole image back
down from Telegram to draw a single icon.
"""

import contextlib
import io as _io
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import gamestage  # noqa: E402
import tgio  # noqa: E402
import tgupload  # noqa: E402
import upload_engine  # noqa: E402
from transfer_models import TransferRequest  # noqa: E402
from media_thumbnail import ThumbnailError, ThumbnailResult  # noqa: E402

PIL = pytest.importorskip("PIL.Image")


def _image(path, size=(1200, 800), colour=(200, 40, 40), fmt="JPEG", **kw):
    img = PIL.new("RGB", size, colour)
    # Noise, so the JPEG does not compress to almost nothing and the size
    # ceiling below is actually exercised by something.
    for x in range(0, size[0], 3):
        for y in range(0, size[1], 3):
            img.putpixel((x, y), ((x * 7) % 256, (y * 13) % 256, (x + y) % 256))
    img.save(path, fmt, **kw)
    return path


# --------------------------------------------------------------------------- #
# the preview itself
# --------------------------------------------------------------------------- #


def test_a_photo_yields_a_jpeg_within_telegrams_limits(tmp_path):
    made = tgio.make_preview(_image(tmp_path / "photo.jpg"))

    assert made is not None
    data, width, height = made
    # The dimensions are the *original* file's — they are what /rpc/props
    # reports, so the shell never opens the file to measure it.
    assert (width, height) == (1200, 800)
    thumb = PIL.open(_io.BytesIO(data))
    assert thumb.format == "JPEG"  # anything else Telegram silently drops
    assert max(thumb.size) <= tgio.PREVIEW_BOX
    assert len(data) <= tgio.PREVIEW_MAX_BYTES


def test_a_png_with_alpha_survives_the_jpeg_conversion(tmp_path):
    path = tmp_path / "sticker.png"
    PIL.new("RGBA", (600, 600), (0, 128, 255, 128)).save(path, "PNG")

    made = tgio.make_preview(path)

    assert made is not None and made[1:] == (600, 600)
    assert PIL.open(_io.BytesIO(made[0])).format == "JPEG"


def test_exif_orientation_is_reported_as_the_viewer_sees_it(tmp_path):
    """A sideways photo's dimensions are the rotated ones, or Explorer is wrong."""
    path = _image(tmp_path / "rotated.jpg", size=(1200, 800))
    img = PIL.open(path)
    exif = img.getexif()
    exif[274] = 6  # rotate 90 CW
    img.save(path, "JPEG", exif=exif)

    made = tgio.make_preview(path)

    assert made[1:] == (800, 1200)


def test_a_zip_has_no_preview(tmp_path):
    """The /game path uploads archives; there is nothing to decode."""
    blob = tmp_path / "game.zip"
    blob.write_bytes(bytes([0x50, 0x4B, 3, 4]) + bytes(4096))

    assert tgio.make_preview(blob) is None


def test_a_truncated_image_does_not_break_the_upload(tmp_path):
    """A preview is a nicety. Failing to make one must never fail an upload."""
    path = tmp_path / "half.jpg"
    path.write_bytes(_image(tmp_path / "whole.jpg").read_bytes()[:200])

    assert tgio.make_preview(path) is None


def test_make_preview_adapts_a_ready_media_thumbnail(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    seen = []

    def capture(candidate, mime_type, ffmpeg):
        seen.append((candidate, mime_type, ffmpeg))
        return ThumbnailResult("ready", b"jpeg", 640, 360)

    monkeypatch.setattr(tgio, "capture_thumbnail", capture)

    assert tgio.make_preview(path, "video/mp4", "configured-ffmpeg") == (b"jpeg", 640, 360)
    assert seen == [(path, "video/mp4", "configured-ffmpeg")]


def test_make_preview_does_not_hide_decodable_media_capture_errors(tmp_path, monkeypatch):
    def fail(*_):
        raise ThumbnailError("no thumbnail")

    monkeypatch.setattr(tgio, "capture_thumbnail", fail)

    with pytest.raises(ThumbnailError, match="no thumbnail"):
        tgio.make_preview(tmp_path / "clip.mp4", "video/mp4", "configured-ffmpeg")


# --------------------------------------------------------------------------- #
# what reaches the message
# --------------------------------------------------------------------------- #
#
# Through the engine's non-album path -- the one /game packs and every split
# segment take. The album path attaches its preview during preparation instead
# and is covered in test_upload_album.py.


class _Worker:
    user_id = 1

    def __init__(self):
        self.calls = []

    def prepare_segment(self, stream, size, name, progress=None, *, force_big=None):
        return {"size": size}

    def prepare_thumbnail(self, preview):
        # The path has to still exist while the upload is running: it is a real
        # file on disk because Telethon uploads a thumbnail by name and Telegram
        # ignores one that is not a .jpg.
        assert preview[0].exists() and preview[0].suffix == ".jpg"
        return preview

    def send_uploaded_segment(self, handle, size, name, preview=None, *, mime_type=None, message_limiter=None):
        self.calls.append((name, preview))
        return {"message_id": 1, "file_id": "f", "access_hash": "h", "size": size}


class _Pool:
    def __init__(self, worker):
        self.runtime = SimpleNamespace(
            worker=worker, telegram_user_id=worker.user_id,
            file_slots=threading.BoundedSemaphore(1), message_limiter=None,
        )

    @contextlib.contextmanager
    def acquire_upload(self, timeout=None):
        yield self.runtime


class _Api:
    def __init__(self):
        self.registered = []

    def check_hash(self, file_hash):
        return {"found": False, "files": []}

    def register(self, **row):
        self.registered.append(row)

    def invalidate(self, parent_id=None):
        pass


def _transfer(path, name, mime, api=None):
    """One non-album transfer; returns the worker that saw the messages."""
    worker = _Worker()
    engine = upload_engine.UploadEngine(api or _Api(), _Pool(worker))
    request = TransferRequest(
        Path(path), name, mime, "pid", path.stat().st_size, allow_album=False,
    )
    return worker, engine, engine.transfer(request)


def test_an_image_upload_carries_a_preview_and_its_size(tmp_path):
    path = _image(tmp_path / "photo.jpg")

    worker, _engine, _result = _transfer(path, "photo.jpg", "image/jpeg")

    (name, preview), = worker.calls
    assert name == "photo.jpg"
    # The original's dimensions, not the thumbnail's: that is what /rpc/props
    # answers with, and Telegram drops a thumbnail with no declared size.
    assert preview[1:] == (1200, 800)


def test_a_video_upload_carries_a_preview_and_its_mime_type(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"video bytes")
    seen = []

    def preview(candidate, mime_type, ffmpeg=None):
        seen.append((candidate, mime_type))
        return b"jpeg", 640, 360

    monkeypatch.setattr(gamestage, "make_preview", preview)

    worker, _engine, _result = _transfer(path, "clip.mp4", "video/mp4")

    assert seen == [(path, "video/mp4")]
    assert worker.calls[0][1][1:] == (640, 360)


def test_a_non_image_upload_carries_none(tmp_path):
    blob = tmp_path / "game.zip"
    blob.write_bytes(bytes([0x50, 0x4B, 3, 4]) + bytes(4096))

    worker, _engine, _result = _transfer(blob, "game.zip", "application/zip")

    assert worker.calls == [("game.zip", None)]


def test_a_split_upload_carries_none_beyond_the_first_part(tmp_path, monkeypatch):
    """entry.message_id points at part 0, but a part is not an image.

    Attaching an image preview and DocumentAttributeImageSize to a slice of a
    larger file would be describing something that does not exist.
    """
    monkeypatch.setattr(tgupload, "SMALL_FILE_MAX", 4096)
    monkeypatch.setattr(tgupload, "MESSAGE_MAX", 4096)
    path = _image(tmp_path / "huge.jpg")

    worker, _engine, result = _transfer(path, "huge.jpg", "image/jpeg")

    assert len(worker.calls) > 1
    assert [part.has_thumbnail for part in result.parts][1:] == [False] * (len(result.parts) - 1)
    assert {preview for name, preview in worker.calls if name != "huge.jpg.part1"} == {None}


def test_the_temp_preview_is_gone_afterwards(tmp_path):
    path = _image(tmp_path / "photo.jpg")

    worker, _engine, _result = _transfer(path, "photo.jpg", "image/jpeg")

    (_, preview), = worker.calls
    assert not preview[0].exists()


# --------------------------------------------------------------------------- #
# admitting to the preview
# --------------------------------------------------------------------------- #


def test_an_uploaded_image_is_registered_as_having_a_thumbnail(tmp_path):
    """Attaching the preview is only half of it; the row has to say so.

    ``Resolver.thumbs_for`` and ``needs_warming`` both refuse to look for a
    preview when the row says there is none, so a hard-coded False made
    /rpc/thumb answer 404 off the flag in 0.12s -- without ever asking Telegram,
    which by then did have the thumbnail. The shell handler cannot tell that
    from "fetch failed" and delegates, and the built-in handler reads the whole
    image. Both halves or neither.
    """
    path = _image(tmp_path / "photo.jpg")
    api = _Api()

    _worker, engine, result = _transfer(path, "photo.jpg", "image/jpeg", api)
    engine.register_result(result)

    (row,) = api.registered
    assert row["has_thumbnail"] is True


def test_a_zip_is_registered_as_having_none(tmp_path):
    blob = tmp_path / "game.zip"
    blob.write_bytes(bytes([0x50, 0x4B, 3, 4]) + bytes(4096))
    api = _Api()

    _worker, engine, result = _transfer(blob, "game.zip", "application/zip", api)
    engine.register_result(result)

    assert api.registered[0]["has_thumbnail"] is False


def test_dedup_keeps_whatever_the_reused_message_already_had(tmp_path):
    """A dedup registration points at the same Telegram message, thumbnail and all."""
    rows = [{
        "filesize": 4096, "mime_type": "image/jpeg", "telegram_message_id": 77,
        "access_hash": "ah", "is_split_file": False, "has_thumbnail": True,
    }]

    (part,) = upload_engine.canonical_existing_parts(rows, original_size=4096)

    assert part.has_thumbnail is True


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
