"""What one finished transfer says in bridge.log, and what it must never say.

This log line is the only view of where a transfer's time actually went, and
bridge.log is the diagnostic tool every trap in CLAUDE.md was found with. It is
also written to disk and survives the process, so a credential that reaches it
has leaked -- which is why the redaction happens at the point the text is
built, not at the point somebody reads it.
"""

from __future__ import annotations

import contextlib
import logging
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import upload_engine  # noqa: E402
from transfer_models import TransferRequest  # noqa: E402

TIMING_FIELDS = (
    "protocol=", "bytes=", "parts=", "hash_ms=", "check_ms=", "thumb_ms=",
    "slot_ms=", "upload_ms=", "message_ms=", "register_ms=", "total_ms=",
    "accounts=", "rate=", "ceiling=",
)


class Worker:
    user_id = 5

    def prepare_segment(self, stream, size, name, progress=None, *, force_big=None):
        return {"size": size}

    def prepare_thumbnail(self, preview):
        return preview

    def send_uploaded_segment(self, handle, size, name, preview=None, *, mime_type=None, message_limiter=None):
        return {"message_id": 70, "file_id": "doc", "access_hash": "ah", "size": size}


class Limiter:
    def stats(self):
        return {"rate": 3.5, "ceiling": 8.0, "window": 2, "floods": 1}


class Pool:
    def __init__(self):
        self.runtime = SimpleNamespace(
            worker=Worker(), telegram_user_id=Worker.user_id,
            file_slots=threading.BoundedSemaphore(1),
            message_limiter=None, chunk_limiter=Limiter(),
        )

    @contextlib.contextmanager
    def acquire_upload(self, timeout=None):
        yield self.runtime


class Api:
    def __init__(self):
        self.rows = []
        self.registered = []

    def check_hash(self, fingerprint):
        return {"found": bool(self.rows), "files": self.rows}

    def register(self, **row):
        self.registered.append(row)

    def invalidate(self, parent_id=None):
        pass


@pytest.fixture
def rig(tmp_path):
    api = Api()
    engine = upload_engine.UploadEngine(api, Pool())

    def request(size=1024, name="file.bin", mime="application/octet-stream"):
        path = tmp_path / name
        path.write_bytes(b"x" * size)
        return TransferRequest(path, name, mime, "parent", size, allow_album=False)

    return SimpleNamespace(engine=engine, api=api, request=request)


def _line(caplog, needle):
    return next(r.getMessage() for r in caplog.records if needle in r.getMessage())


def test_completion_log_contains_every_web_timing_field(rig, caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")

    rig.engine.register_result(rig.engine.transfer(rig.request(size=1024)))

    line = _line(caplog, "transfer complete")
    for field in TIMING_FIELDS:
        assert field in line, f"{field} missing from {line}"
    assert "protocol=small" in line
    assert "bytes=1024" in line and "parts=1" in line
    assert "accounts=(5,)" in line or "accounts=[5]" in line
    assert "rate=3.50" in line and "ceiling=8.0" in line


def test_a_reused_duplicate_reports_no_upload_time(rig, caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")
    rig.api.rows = [{
        "telegram_message_id": 78, "file_id": "98", "filesize": 10,
        "telegram_user_id": 42, "has_thumbnail": False,
    }]

    rig.engine.register_result(rig.engine.transfer(rig.request(size=10)))

    line = _line(caplog, "transfer complete")
    # The check still cost a round trip; the bytes did not move.
    assert "protocol=duplicate" in line
    assert "upload_ms=0" in line and "message_ms=0" in line
    assert "accounts=(42,)" in line


def test_a_failure_is_logged_once_with_the_error_redacted(rig, caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")
    secret = "1AaBbCcSession"

    def explode(fingerprint):
        raise RuntimeError(
            f"session={secret} rejected; Authorization: Bearer eyJhbG.cCI6.Ikp9"
        )

    rig.api.check_hash = explode
    with pytest.raises(RuntimeError):
        rig.engine.transfer(rig.request())

    assert secret not in caplog.text
    assert "eyJhbG.cCI6.Ikp9" not in caplog.text
    assert "transfer failed" in caplog.text
    assert "RuntimeError" in caplog.text


def test_timing_fields_are_numbers_a_reader_can_add_up(rig, caplog):
    caplog.set_level(logging.INFO, logger="upload_engine")

    rig.engine.register_result(rig.engine.transfer(rig.request(size=2048)))

    line = _line(caplog, "transfer complete")
    fields = dict(
        pair.split("=", 1) for pair in line.split() if "=" in pair
    )
    stages = ("hash_ms", "check_ms", "thumb_ms", "slot_ms", "upload_ms",
              "message_ms", "register_ms")
    for name in stages + ("total_ms",):
        assert float(fields[name]) >= 0.0
    # total is the wall clock of the whole thing, so it cannot be less than any
    # single stage it contains.
    assert float(fields["total_ms"]) >= max(float(fields[name]) for name in stages)


def test_the_example_configuration_carries_no_credentials():
    """The two files a new operator copies must be safe to paste anywhere.

    Checked structurally rather than by scanning for the word: both files have
    to *explain* what a session string is, and a text scan that cannot tell a
    sentence from a value would push that explanation out of the file people
    actually read.
    """
    import configparser
    import json as _json

    root = Path(__file__).resolve().parents[1]

    accounts = _json.loads((root / "accounts.example.json").read_text(encoding="utf-8"))
    assert accounts["accounts"], "the example must still show the shape"
    for account in accounts["accounts"]:
        assert account["session"] == ""
        assert set(account) == {"telegram_user_id", "label", "session"}

    secrets = {"session", "api_hash", "api_id", "token", "password", "jwt"}
    parser = configparser.ConfigParser()
    parser.read(root / "config.example.ini", encoding="utf-8")
    for section in parser.sections():
        for key, value in parser.items(section):
            if key in secrets:
                assert value.strip() == "", f"{section}.{key} carries a value"
