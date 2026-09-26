"""One pool and one engine behind /game, /rpc/status and warmup.

The transfer half of this project now has exactly one owner. These tests pin
the seams where the rest of the application reaches it: what /game asks the
engine for, what /rpc/status is allowed to say about it, and that the read
path a sweep uses is routed by account rather than by whichever worker
happened to be handed in.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bridge  # noqa: E402
import gamestage  # noqa: E402
import upload_engine  # noqa: E402
import warmup  # noqa: E402
from config import Config  # noqa: E402
from telegram_accounts import AccountUnavailableError, TelegramAccountPool  # noqa: E402
from transfer_models import AccountSpec, TransferResult, UploadedPart  # noqa: E402


class RecordingEngine:
    """Stands in for UploadEngine: remembers what it was asked to transfer."""

    def __init__(self):
        self.requests = []
        self.registered = []

    def transfer(self, request):
        self.requests.append(request)
        return TransferResult(request, "fingerprint", (
            UploadedPart(0, 900, "doc", "ah", request.logical_size, 1),
        ))

    def register_result(self, result):
        self.registered.append(result)


class Api:
    def __init__(self):
        self.folders = {}
        self.invalidated = []

    def ensure_folder(self, name):
        self.folders.setdefault(name, SimpleNamespace(file_id=f"id-{name}"))
        return self.folders[name]

    def invalidate(self, parent_id=None):
        self.invalidated.append(parent_id)

    def resolve(self, segments):
        return None


@pytest.fixture
def cfg(tmp_path):
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    (session_dir / "1.session").write_bytes(b"sqlite")
    built = Config(
        api_id=1, api_hash="hash", primary_user_id=1, session_dir=session_dir,
        base_url="http://backend",
        game_folder="game", dir_cache_seconds=60.0, host="127.0.0.1", port=0,
        mount_drive="E:", log_level="WARNING",
        cache_dir=tmp_path / "cache", local_dir=tmp_path / "local",
        staging_dir=tmp_path / "staging", upload_dir=tmp_path / "uploads",
        debounce_minutes=0.0,
    )
    for path in (built.cache_dir, built.local_dir, built.staging_dir, built.pack_dir, built.upload_dir):
        path.mkdir(parents=True, exist_ok=True)
    return built


@pytest.fixture
def game(cfg):
    api = Api()
    engine = RecordingEngine()
    stager = gamestage.GameStager(cfg, api, engine)
    return SimpleNamespace(cfg=cfg, api=api, engine=engine, stager=stager)


# --------------------------------------------------------------------------- #
# /game
# --------------------------------------------------------------------------- #


def test_a_packed_directory_is_transferred_as_a_zip_with_no_album(game):
    tree = game.cfg.staging_dir / "Title"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "run.exe").write_bytes(b"MZ" * 100)
    game.stager.touch("Title")

    game.stager._process("Title")

    request = game.engine.requests[0]
    assert request.upload_name == "Title.zip"
    assert request.mime_type == "application/zip"
    # An archive is not media: it has nothing to preview and nothing to group.
    assert request.allow_album is False
    assert request.parent_id == "id-game"
    assert game.engine.registered[0].request is request


def test_a_top_level_file_keeps_its_own_type_and_album_eligibility(game):
    (game.cfg.staging_dir / "shot.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"jpeg" * 50)
    game.stager.touch("shot.jpg")

    game.stager._process("shot.jpg")

    request = game.engine.requests[0]
    assert request.upload_name == "shot.jpg"
    # Not application/zip: a file dropped into /game is uploaded verbatim, so
    # registering it as an archive would cost it its preview on both clients.
    assert request.mime_type == "image/jpeg"
    assert request.allow_album is True


def test_a_top_level_archive_is_still_an_archive(game):
    (game.cfg.staging_dir / "Manual.zip").write_bytes(b"PK\x03\x04" + b"\0" * 40)
    game.stager.touch("Manual.zip")

    game.stager._process("Manual.zip")

    # "application/zip", not the machine-local "application/x-zip-compressed"
    # Windows' registry answers with: the row's mime type is what the web
    # client reads back, and what album eligibility is decided from.
    request = game.engine.requests[0]
    assert request.mime_type == "application/zip"
    assert upload_engine.album_eligible(request.mime_type, request.logical_size) is False


def test_a_failed_transfer_keeps_the_staged_tree(game):
    tree = game.cfg.staging_dir / "Title"
    tree.mkdir(parents=True)
    (tree / "a.bin").write_bytes(b"x" * 10)
    game.stager.touch("Title")

    def explode(request):
        raise RuntimeError("upload failed")

    game.engine.transfer = explode
    game.stager._process("Title")

    assert tree.exists()
    assert game.stager.status()["units"][0]["state"] == "failed"


# --------------------------------------------------------------------------- #
# /rpc/status
# --------------------------------------------------------------------------- #


class Limiter:
    def __init__(self):
        self.session = "1AaBbCcSecretSessionString"

    def stats(self):
        return {
            "rate": 4.0, "ceiling": None, "window": 2, "floods": 0,
            "mode": "normal", "clean_window_start": None,
        }


def _rpc_status(app) -> dict:
    captured = {}

    def start_response(status, headers):
        captured["status"] = status

    body = b"".join(app(
        {"PATH_INFO": "/rpc/status", "REQUEST_METHOD": "GET"}, start_response,
    ))
    assert captured["status"].startswith("200")
    return json.loads(body.decode("utf-8"))


@pytest.fixture
def rpc(cfg):
    workers = {
        1: SimpleNamespace(user_id=1, stop=lambda: None),
        2: SimpleNamespace(user_id=2, stop=lambda: None),
    }
    pool = TelegramAccountPool(
        [AccountSpec(1, Path("/sessions/1.session")), AccountSpec(2, Path("/sessions/2.session"))],
        api_id=1, api_hash="hash",
        worker_factory=lambda _a, _b, user_id, _path, *_args, **_kwargs: workers[user_id],
        chunk_limiter_factory=Limiter,
    )
    for identity in (1, 2):
        pool.runtime(identity).online = pool.runtime(identity).linked = True
    api = Api()
    engine = RecordingEngine()
    resolver = bridge.Resolver(cfg, api, pool)
    stager = gamestage.GameStager(cfg, api, engine)
    from uploadstage import UploadStager

    upload_stager = UploadStager(cfg, api, engine)
    app = bridge.RpcApp(cfg, resolver, None, stager, upload_stager)
    return SimpleNamespace(app=app, pool=pool, stager=stager, upload_stager=upload_stager)


def test_status_reports_every_account_and_its_limiter(rpc):
    body = _rpc_status(rpc.app)
    assert [account["telegram_user_id"] for account in body["accounts"]] == [1, 2]
    assert {"rate", "ceiling", "window", "floods", "mode"} <= set(body["accounts"][0]["limiter"])
    assert {"idle", "active_byte_upload_jobs", "in_flight_upload_rpcs"} <= set(body["accounts"][0])
    assert body["eligible_upload_ids"] == [1, 2]
    # The two queues stay distinguishable: /game packs, everything else does not.
    assert "units" in body and "pending" in body["uploads"]
    assert "schedulers" in body["uploads"]


def test_status_never_renders_a_credential(rpc):
    rendered = json.dumps(_rpc_status(rpc.app)).lower()
    for secret in ("session", "bearer", "jwt", "1aabbcc"):
        assert secret not in rendered


# --------------------------------------------------------------------------- #
# Lifecycle and routed reads
# --------------------------------------------------------------------------- #


def test_only_the_primary_answers_the_bot_challenge(cfg):
    started = []
    workers = {
        identity: SimpleNamespace(
            user_id=identity, stop=lambda: None,
            start=lambda i=identity: started.append(i),
            send_dm=lambda username, text, i=identity: None,
        )
        for identity in (1, 2)
    }
    pool = TelegramAccountPool(
        [AccountSpec(1, Path("/sessions/1.session")), AccountSpec(2, Path("/sessions/2.session"))],
        api_id=1, api_hash="hash",
        worker_factory=lambda _a, _b, user_id, _path, *_args, **_kwargs: workers[user_id],
    )
    senders = []
    api = SimpleNamespace(
        set_dm_sender=senders.append,
        login=lambda: None,
        linked_account_ids=lambda: {1, 2},
    )
    pool.start(api)
    assert started == [1, 2]
    # One DM sender, and it is the primary's: the challenge proves the drive
    # owner's identity, not whichever account happens to store a segment.
    assert senders == [workers[1].send_dm]


def test_stopping_the_pool_stops_every_account(cfg):
    stopped = []
    workers = {
        identity: SimpleNamespace(
            user_id=identity, start=lambda: None,
            stop=lambda i=identity: stopped.append(i),
            send_dm=lambda *_a: None,
        )
        for identity in (1, 2)
    }
    pool = TelegramAccountPool(
        [AccountSpec(1, Path("/sessions/1.session")), AccountSpec(2, Path("/sessions/2.session"))],
        api_id=1, api_hash="hash",
        worker_factory=lambda _a, _b, user_id, _path, *_args, **_kwargs: workers[user_id],
    )
    pool.start(SimpleNamespace(
        set_dm_sender=lambda _s: None, login=lambda: None,
        linked_account_ids=lambda: {1, 2},
    ))
    pool.stop()
    assert sorted(stopped) == [1, 2]


def test_warmup_reads_previews_and_properties_through_the_owning_account(cfg):
    """A sweep must not ask the primary for a segment another account stores."""
    asked = {1: [], 2: []}

    def worker(identity):
        return SimpleNamespace(
            user_id=identity, stop=lambda: None,
            thumbnails=lambda parts, i=identity: (
                asked[i].append([p.message_id for p in parts])
                or {(p.message_id, str(p.file_id)): b"\xff\xd8jpeg" for p in parts}
            ),
            media_info=lambda parts, i=identity: (
                asked[i].append([p.message_id for p in parts])
                or {(p.message_id, str(p.file_id)): {"width": 4, "height": 2} for p in parts}
            ),
        )

    workers = {1: worker(1), 2: worker(2)}
    pool = TelegramAccountPool(
        [AccountSpec(1, Path("/sessions/1.session")), AccountSpec(2, Path("/sessions/2.session"))],
        api_id=1, api_hash="hash",
        worker_factory=lambda _a, _b, user_id, _path, *_args, **_kwargs: workers[user_id],
    )
    for identity in (1, 2):
        pool.runtime(identity).online = pool.runtime(identity).linked = True

    from tdapi import Entry

    entries = [
        Entry(file_id="a", name="a.jpg", is_dir=False, size=4, mtime=0.0, mime="image/jpeg",
              message_id=11, has_thumbnail=True, telegram_user_id=1),
        Entry(file_id="b", name="b.jpg", is_dir=False, size=4, mtime=0.0, mime="image/jpeg",
              message_id=22, has_thumbnail=True, telegram_user_id=2),
    ]
    api = SimpleNamespace(
        parts_for=lambda entry: [SimpleNamespace(
            message_id=entry.message_id, size=entry.size,
            telegram_user_id=entry.telegram_user_id, file_id=entry.file_id,
        )],
        invalidate=lambda *_a: None,
    )
    resolver = bridge.Resolver(cfg, api, pool)
    (cfg.cache_dir / "thumbs").mkdir(parents=True, exist_ok=True)

    resolver.thumbs_for(entries)
    resolver.props_for(entries, demand=False)

    assert asked[1] and all(11 in batch for batch in asked[1])
    assert asked[2] and all(22 in batch for batch in asked[2])
    assert not any(22 in batch for batch in asked[1])


def test_a_row_with_no_account_still_reads_from_the_primary(cfg):
    primary = SimpleNamespace(user_id=7, stop=lambda: None)
    pool = TelegramAccountPool(
        [AccountSpec(7, Path("/sessions/7.session"))], api_id=1, api_hash="hash",
        worker_factory=lambda *_a, **_kw: primary,
    )
    pool.primary.online = pool.primary.linked = True
    assert pool.for_read(0).worker is primary
    with pytest.raises(AccountUnavailableError):
        pool.for_read(99)


def test_warmup_cli_builds_a_pool_rather_than_one_worker():
    """warmup.py's CLI hands its client to Resolver, which now routes by account."""
    import inspect

    source = inspect.getsource(warmup.main)
    assert "TelegramAccountPool" in source
    assert "Resolver(cfg, api, pool)" in source
