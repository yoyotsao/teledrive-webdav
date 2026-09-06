"""Offline contracts for still and video upload thumbnails."""

import io
import sys
from pathlib import Path
from subprocess import CompletedProcess

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from media_thumbnail import (  # noqa: E402
    ThumbnailError,
    capture_thumbnail,
    discover_ffmpeg,
)

PIL = pytest.importorskip("PIL.Image")


def _image(path, size=(1200, 800), mode="RGB", colour=(200, 40, 40), fmt="JPEG"):
    image = PIL.new(mode, size, colour)
    image.save(path, fmt)
    return path


@pytest.fixture
def fake_ffmpeg(tmp_path, monkeypatch):
    """Offline replacement for the bounded, shell-free ffmpeg boundary."""
    jpeg = _image(tmp_path / "frame.jpg", size=(640, 360))
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return CompletedProcess(args, 0, jpeg.read_bytes(), b"")

    monkeypatch.setattr("media_thumbnail.subprocess.run", run)
    return calls


def test_still_thumbnail_is_oriented_flattened_and_bounded(tmp_path):
    path = _image(tmp_path / "rotated.jpg", size=(1200, 800))
    image = PIL.open(path)
    exif = image.getexif()
    exif[274] = 6
    image.save(path, "JPEG", exif=exif)

    result = capture_thumbnail(path, "image/jpeg", None)

    assert result.kind == "ready"
    assert (result.width, result.height) == (800, 1200)
    thumbnail = PIL.open(io.BytesIO(result.jpeg))
    assert thumbnail.format == "JPEG"
    assert max(thumbnail.size) <= 320
    assert len(result.jpeg) <= 20 * 1024


def test_alpha_webp_is_ready(tmp_path):
    path = _image(tmp_path / "sticker.webp", size=(600, 600), mode="RGBA", colour=(0, 128, 255, 128), fmt="WEBP")

    result = capture_thumbnail(path, "image/webp", None)

    assert result.kind == "ready"
    assert PIL.open(io.BytesIO(result.jpeg)).format == "JPEG"


def test_non_media_is_not_media(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("plain text", encoding="utf-8")

    assert capture_thumbnail(path, "text/plain", None).kind == "not_media"


def test_truncated_still_is_undecodable(tmp_path):
    path = tmp_path / "half.jpg"
    path.write_bytes(_image(tmp_path / "whole.jpg").read_bytes()[:100])

    assert capture_thumbnail(path, "image/jpeg", None).kind == "undecodable"


def test_decodable_video_uses_configured_ffmpeg_without_a_shell(tmp_path, fake_ffmpeg):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"pretend video")

    result = capture_thumbnail(path, "video/mp4", "fake-ffmpeg")

    assert result.kind == "ready"
    assert result.jpeg.startswith(b"\xff\xd8")
    assert max(result.width, result.height) > 0
    args, kwargs = fake_ffmpeg[0]
    assert args[0] == "fake-ffmpeg"
    assert "-frames:v" in args and args[args.index("-frames:v") + 1] == "1"
    assert "scale=320:320:force_original_aspect_ratio=decrease" in args
    assert kwargs["shell"] is False
    assert kwargs["timeout"] > 0


def test_unsupported_video_codec_is_explicitly_undecodable(tmp_path, fake_ffmpeg, monkeypatch):
    def invalid(args, **kwargs):
        return CompletedProcess(args, 1, b"", b"Invalid data found when processing input")

    monkeypatch.setattr("media_thumbnail.subprocess.run", invalid)
    path = tmp_path / "bad.mkv"
    path.write_bytes(b"unsupported codec")

    result = capture_thumbnail(path, "video/x-matroska", "fake-ffmpeg")

    assert result.kind == "undecodable"
    assert "Invalid data" in result.error


def test_video_that_decodes_but_yields_no_frame_raises_capture_error(tmp_path, fake_ffmpeg, monkeypatch):
    def empty(args, **kwargs):
        return CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr("media_thumbnail.subprocess.run", empty)
    path = tmp_path / "empty.mp4"
    path.write_bytes(b"decodable but no frame")

    with pytest.raises(ThumbnailError, match="no thumbnail"):
        capture_thumbnail(path, "video/mp4", "fake-ffmpeg")


def test_ffmpeg_discovery_uses_config_then_environment_then_path(monkeypatch):
    monkeypatch.setenv("FFMPEG", "from-environment")
    monkeypatch.setattr("media_thumbnail.shutil.which", lambda command: "from-path")

    assert discover_ffmpeg("configured") == "configured"
    assert discover_ffmpeg("") == "from-environment"
    monkeypatch.delenv("FFMPEG")
    assert discover_ffmpeg("") == "from-path"
