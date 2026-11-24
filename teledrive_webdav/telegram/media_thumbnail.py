"""Bounded still-image and video thumbnail capture for Telegram documents."""

from __future__ import annotations

import io
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional


PREVIEW_BOX = 320
PREVIEW_MAX_BYTES = 20 * 1024
FFMPEG_TIMEOUT_SECONDS = 15


class ThumbnailError(RuntimeError):
    """A decodable media file could not produce a usable thumbnail."""


@dataclass(frozen=True)
class ThumbnailResult:
    kind: Literal["ready", "not_media", "undecodable"]
    jpeg: Optional[bytes] = None
    width: Optional[int] = None
    height: Optional[int] = None
    error: Optional[str] = None


def discover_ffmpeg(configured: str | None) -> Optional[str]:
    """Return the configured binary, then FFMPEG, then the PATH candidate."""
    candidate = configured or os.environ.get("FFMPEG", "") or shutil.which("ffmpeg") or ""
    return candidate or None


def capture_thumbnail(path, mime_type: str, ffmpeg: str | None) -> ThumbnailResult:
    """Capture one Telegram-safe JPEG, preserving media classification.

    Non-media never reaches a decoder.  An image or video that its decoder
    cannot read is explicitly ``undecodable``; a decoder that succeeds but
    cannot supply a frame raises ``ThumbnailError`` so callers do not silently
    register decodable media without the preview expected by the web client.
    """
    mime = (mime_type or "").split(";", 1)[0].strip().lower()
    if mime.startswith("image/"):
        return _capture_still(Path(path))
    if mime.startswith("video/"):
        return _capture_video(Path(path), ffmpeg)
    return ThumbnailResult("not_media")


def _capture_still(path: Path) -> ThumbnailResult:
    try:
        from PIL import Image, ImageOps

        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            image.load()
            width, height = image.size
            return ThumbnailResult("ready", _encode_jpeg(image), width, height)
    except ImportError:
        return ThumbnailResult("undecodable", error="Pillow is not installed")
    except Exception as exc:
        return ThumbnailResult("undecodable", error=str(exc))


def _capture_video(path: Path, configured_ffmpeg: str | None) -> ThumbnailResult:
    ffmpeg = discover_ffmpeg(configured_ffmpeg)
    if ffmpeg is None:
        return ThumbnailResult("undecodable", error="ffmpeg is unavailable")

    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-ss",
        "00:00:01",
        "-i",
        os.fspath(path),
        "-frames:v",
        "1",
        "-vf",
        "scale=320:320:force_original_aspect_ratio=decrease",
        "-an",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(
            command,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=FFMPEG_TIMEOUT_SECONDS,
            shell=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise ThumbnailError(f"ffmpeg timed out while capturing {path.name}") from exc
    except OSError as exc:
        raise ThumbnailError(f"could not run ffmpeg for {path.name}: {exc}") from exc

    stderr = completed.stderr.decode("utf-8", "replace").strip()
    if completed.returncode:
        if _undecodable_ffmpeg(stderr):
            return ThumbnailResult("undecodable", error=stderr or "ffmpeg could not decode media")
        raise ThumbnailError(_ffmpeg_failure(path, stderr))
    if not completed.stdout:
        raise ThumbnailError(f"ffmpeg produced no thumbnail for {path.name}")

    try:
        from PIL import Image

        with Image.open(io.BytesIO(completed.stdout)) as frame:
            frame.load()
            width, height = frame.size
            return ThumbnailResult("ready", _encode_jpeg(frame), width, height)
    except ImportError:
        return ThumbnailResult("undecodable", error="Pillow is not installed")
    except Exception as exc:
        raise ThumbnailError(f"ffmpeg produced an invalid thumbnail for {path.name}: {exc}") from exc


def _encode_jpeg(image) -> bytes:
    """Flatten and recompress an image to Telegram's 320px/20KiB contract."""
    from PIL import Image

    rgba = image.convert("RGBA")
    flattened = Image.new("RGBA", rgba.size, "white")
    flattened.alpha_composite(rgba)
    bounded = flattened.convert("RGB")
    resampling = getattr(Image, "Resampling", Image).LANCZOS
    bounded.thumbnail((PREVIEW_BOX, PREVIEW_BOX), resampling)

    while True:
        for quality in (85, 75, 65, 55, 45, 35, 25, 15, 10):
            output = io.BytesIO()
            bounded.save(output, "JPEG", quality=quality, optimize=True)
            data = output.getvalue()
            if len(data) <= PREVIEW_MAX_BYTES:
                return data
        if max(bounded.size) == 1:
            return data  # pragma: no cover - a 1px JPEG is always under the cap
        bounded.thumbnail(
            (max(1, bounded.width * 3 // 4), max(1, bounded.height * 3 // 4)),
            resampling,
        )


def _undecodable_ffmpeg(stderr: str) -> bool:
    message = stderr.lower()
    return any(
        marker in message
        for marker in (
            "invalid data",
            "unsupported",
            "unknown decoder",
            "could not find codec",
            "codec not found",
            "not yet implemented",
        )
    )


def _ffmpeg_failure(path: Path, stderr: str) -> str:
    detail = f": {stderr}" if stderr else ""
    return f"ffmpeg could not capture a thumbnail for {path.name}{detail}"
