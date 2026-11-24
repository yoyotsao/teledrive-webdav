"""Logs and status must identify drive files by id, never by name.

The unit tests here cover the output-side backstop (``logscrub``); the
whole-bridge guard that drives real scenarios lives at the bottom.
"""
import io
import logging
import sys
import threading
from pathlib import Path
from urllib.parse import quote

import pytest

from fileid import path_id, tag, tag_dav
import logscrub

MARK = "PRIVATE-名前-123.jpg"
FMT = "%(asctime)s %(levelname)-7s %(name)-10s %(message)s"


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


@pytest.fixture
def capture(cache):
    """A logger whose handler formats through ScrubbingFormatter."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logscrub.ScrubbingFormatter(FMT, cache_dir=cache, drive="H:"))
    logger = logging.getLogger("bridge.test_privacy")
    logger.setLevel(logging.DEBUG)
    logger.addHandler(handler)
    logger.propagate = False
    yield logger, stream
    logger.removeHandler(handler)


def test_formatter_scrubs_oserror_message(capture, cache):
    logger, stream = capture
    try:
        open(cache / "uploads" / "dir" / MARK)
    except OSError as exc:
        logger.warning("copy failed: %s", exc)
    out = stream.getvalue()
    assert "PRIVATE" not in out and "名前" not in out
    assert tag("dir/" + MARK) in out


def test_formatter_scrubs_logged_exception_traceback(capture, cache):
    logger, stream = capture
    try:
        open(str(cache) + "\\staging\\top\\" + MARK)
    except OSError:
        logger.exception("boom")
    out = stream.getvalue()
    assert "Traceback" in out
    assert "PRIVATE" not in out
    assert tag("game/top/" + MARK) in out


def test_formatter_scrubs_local_and_vfs_cache(capture, cache):
    logger, stream = capture
    logger.error("a %s", cache / "local" / "g" / MARK)
    logger.error("b %s", cache / "rclone" / "vfs" / "teledrive" / "x" / MARK)
    out = stream.getvalue()
    assert "PRIVATE" not in out
    assert tag("g/" + MARK) in out
    assert tag("x/" + MARK) in out


def test_formatter_keeps_venv_traceback_paths(capture):
    logger, stream = capture
    logger.error('File "D:\\python\\x\\.venv\\Lib\\site-packages\\telethon\\x.py", line 3')
    assert ".venv\\Lib\\site-packages\\telethon\\x.py" in stream.getvalue()


def test_formatter_maps_drive_paths_and_urls(capture):
    logger, stream = capture
    logger.error("drive H:\\a b\\%s", MARK)
    logger.error("url http://127.0.0.1:8081/%s ok", "%E5%90%8D/x.jpg")
    logger.error("rpc http://127.0.0.1:8081/rpc/thumb?path=H%3A%5Ca%5Cx.jpg&n=1")
    out = stream.getvalue()
    assert "PRIVATE" not in out and "x.jpg" not in out
    assert "drive " + tag("a b/" + MARK) in out
    assert "http://127.0.0.1:8081/" + tag_dav("/%E5%90%8D/x.jpg") in out
    assert "/rpc/thumb?path=" + tag("a/x.jpg") in out and "&n=1" in out


def test_unknown_cache_path_becomes_cache_placeholder(capture, cache):
    logger, stream = capture
    logger.error("saved %s", cache / "savedata-backups" / "g" / MARK)
    out = stream.getvalue()
    assert "PRIVATE" not in out
    assert "<cache>" in out


def test_doubled_backslashes_in_repr_are_handled(capture, cache):
    logger, stream = capture
    logger.error("err %r", str(cache / "uploads" / MARK))
    out = stream.getvalue()
    assert "PRIVATE" not in out
    assert tag(MARK) in out


def test_threading_excepthook_is_scrubbed(capture, cache):
    logger, stream = capture
    old_thread, old_sys = threading.excepthook, sys.excepthook
    try:
        logscrub.install_excepthooks(logger)

        def work():
            raise OSError("cannot read " + str(cache / "uploads" / MARK))

        t = threading.Thread(target=work, name="worker-x")
        t.start()
        t.join()
    finally:
        threading.excepthook, sys.excepthook = old_thread, old_sys
    out = stream.getvalue()
    assert "worker-x" in out
    assert "PRIVATE" not in out
    assert tag(MARK) in out


def test_cheroot_error_log_goes_through_logging(capture, cache):
    logger, stream = capture
    error_log = logscrub.make_cheroot_error_log(logger)
    try:
        raise OSError("bad " + str(cache / "uploads" / MARK))
    except OSError:
        error_log("Error in HTTPRequest", level=40, traceback=True)
    out = stream.getvalue()
    assert "Error in HTTPRequest" in out and "PRIVATE" not in out
    assert tag(MARK) in out


def _fake_cfg(cache):
    import types
    return types.SimpleNamespace(cache_dir=cache / "meta", log_level="INFO", mount_drive="H:")


def test_console_queue_path_is_scrubbed_with_single_timestamp(cache, capsys):
    import bridge

    cache.mkdir(parents=True)
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    listener = bridge.configure_logging(_fake_cfg(cache), cache / "bridge.log")
    try:
        try:
            open(cache / "uploads" / MARK)
        except OSError:
            logging.getLogger("bridge").exception("failed")
    finally:
        listener.stop()
        for h in root.handlers[:]:
            if h not in saved[0]:
                root.removeHandler(h)
                h.close()
        root.setLevel(saved[1])
    console = capsys.readouterr().err
    assert "failed" in console and "PRIVATE" not in console
    assert tag(MARK) in console
    import re

    assert len(re.findall(r"\d{4}-\d\d-\d\d \d\d:", console)) == 1  # one timestamp, not two
    assert "PRIVATE" not in (cache / "bridge.log").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Whole-bridge guard: whatever scenario runs, the marker never reaches a log
# line, stderr, or /rpc/status. This is what keeps a *new* log call from
# quietly putting a file name back.
# --------------------------------------------------------------------------- #

from test_bridge_e2e import _pack_now, _upload_now, rig  # noqa: E402,F401

FOLDER = "PRIVATE-folder-名前"
BAD = ("PRIVATE", "名前")


@pytest.fixture
def prod_log(rig):
    """Everything logged, formatted the way production formats it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(
        logscrub.ScrubbingFormatter(
            FMT, cache_dir=rig.cfg.cache_dir.parent, drive=rig.cfg.mount_drive)
    )
    # The test's own HTTP client (urllib3) and PIL's plugin chatter are not the
    # bridge's output; everything else is, at the most verbose level.
    handler.addFilter(lambda r: not r.name.startswith(("urllib3", "PIL")))
    root = logging.getLogger()
    saved = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)
    root.setLevel(saved)


def _assert_clean(rig, prod_log, capsys):
    text = prod_log.getvalue() + capsys.readouterr().err + rig.request("GET", "/rpc/status").text
    leaked = [b for b in BAD if b in text]
    assert not leaked, f"{leaked} leaked:\n" + "\n".join(
        line for line in text.splitlines() if any(b in line for b in BAD)
    )


def test_guard_general_upload_success_and_failure(rig, prod_log, capsys, monkeypatch):
    assert rig.request("MKCOL", "/" + FOLDER).status_code == 201
    assert rig.request("PUT", f"/{FOLDER}/{MARK}", data=b"x" * 600).status_code == 201
    rig.request("PROPFIND", "/" + FOLDER, headers={"Depth": "1"})
    _upload_now(rig, FOLDER, MARK)

    # a second file whose registration fails -> staging keeps it, failure is logged
    api = rig.resolver.api

    def boom(**kw):
        # a real OSError names the local path, which is what the backstop scrubs
        raise OSError(13, "denied", str(rig.cfg.upload_dir / FOLDER / f"second-{MARK}"))

    monkeypatch.setattr(api, "register", boom)
    assert rig.request("PUT", f"/{FOLDER}/second-{MARK}", data=b"y" * 700).status_code == 201
    try:
        _upload_now(rig, FOLDER, f"second-{MARK}")
    except Exception:
        pass
    _assert_clean(rig, prod_log, capsys)


def test_guard_game_pack_and_zip_reads(rig, prod_log, capsys):
    assert rig.request("MKCOL", f"/game/{FOLDER}").status_code == 201
    assert rig.request("PUT", f"/game/{FOLDER}/{MARK}", data=b"E" * 800).status_code == 201
    _pack_now(rig, FOLDER)
    rig.request("PROPFIND", "/game", headers={"Depth": "1"})
    rig.request("GET", f"/game/{FOLDER}/{MARK}")
    rig.request("GET", f"/game/{FOLDER}/missing-{MARK}")
    _assert_clean(rig, prod_log, capsys)


def test_guard_listing_failure_and_rejected_writes(rig, prod_log, capsys, monkeypatch):
    assert rig.request("MKCOL", "/" + FOLDER).status_code == 201
    api = rig.resolver.api

    def down(*a, **k):
        raise ConnectionError("backend down")

    monkeypatch.setattr(api, "_call", down)
    rig.request("PROPFIND", "/" + FOLDER, headers={"Depth": "1"})
    rig.request("PROPFIND", f"/{FOLDER}/{MARK}", headers={"Depth": "0"})
    rig.request("MOVE", f"/{FOLDER}/{MARK}", headers={"Destination": quote(f"/{FOLDER}/b-{MARK}")})
    _assert_clean(rig, prod_log, capsys)


# --------------------------------------------------------------------------- #
# Specific call sites
# --------------------------------------------------------------------------- #


def test_unknown_label_prints_id_question_mark_never_the_message_id():
    import tgio

    assert tgio.label_for(987654321) == "id=?"
    assert tgio.label_for(None) == "id=?"
    tgio.note_read_label(555, tag("a/b.jpg"))
    assert tgio.label_for(555) == tag("a/b.jpg")


def test_identity_mismatch_error_names_neither_id():
    import tgio

    media = type("M", (), {"id": 111222333})()
    with pytest.raises(tgio.RemoteIdentityError) as err:
        tgio._assert_media_id(media, "444555666", expected_size=1)
    assert "111222333" not in str(err.value) and "444555666" not in str(err.value)


def test_chunk_timing_lists_files_by_id_slowest_first(caplog):
    import time
    import types

    import tgio

    tgio.note_read_label(701, tag("fast/one.bin"))
    tgio.note_read_label(702, tag("slow/two.bin"))
    worker = types.SimpleNamespace(_expected_user_id=1)
    worker._chunk_stats = [0, 0.0, 0.0, time.monotonic(), {}, {}]
    fast = types.SimpleNamespace(id=1, _td_msg=701, dc_id=5, attributes=[])
    slow = types.SimpleNamespace(id=2, _td_msg=702, dc_id=5, attributes=[])
    with caplog.at_level(logging.INFO, logger="tgio"):
        for _ in range(5):
            tgio.TelegramWorker._note_chunk(worker, 0.001, fast)  # busiest, quick
        tgio.TelegramWorker._note_chunk(worker, 2.0, slow)        # one read, very slow
        worker._chunk_stats[3] -= 11  # the 10s window has now elapsed
        tgio.TelegramWorker._note_chunk(worker, 0.001, fast)
    line = next(r.getMessage() for r in caplog.records if "chunk timing" in r.getMessage())
    assert tag("slow/two.bin") in line and tag("fast/one.bin") in line
    assert line.index(tag("slow/two.bin")) < line.index(tag("fast/one.bin"))
    assert "one.bin" not in line and "two.bin" not in line and "doc:" not in line


def test_game_pack_logs_the_id_change_once(rig, prod_log):
    assert rig.request("MKCOL", "/game/NewGame").status_code == 201
    assert rig.request("PUT", "/game/NewGame/run.exe", data=b"E" * 500).status_code == 201
    _pack_now(rig, "NewGame")
    text = prod_log.getvalue()
    # On H: a packed archive shows up as the folder game/NewGame -- the same
    # path, so the same id before and after packing.
    assert f"from={tag('game/NewGame')} to={tag('game/NewGame')}" in text


def test_failed_upload_log_carries_id_and_attempt(rig, prod_log, monkeypatch):
    def boom(**kw):
        raise RuntimeError("backend said no")

    monkeypatch.setattr(rig.resolver.api, "register", boom)
    assert rig.request("PUT", "/retry.bin", data=b"z" * 400).status_code == 201
    for attempt in (1, 2):
        rig.upload_stager._pending[("retry.bin",)].retry_after = 0.0
        try:
            _upload_now(rig, "retry.bin")
        except Exception:
            pass
        assert f"registration failed {tag('retry.bin')} attempt={attempt} " in prod_log.getvalue()


# --------------------------------------------------------------------------- #
# /rpc/status
# --------------------------------------------------------------------------- #


def test_status_identifies_files_by_id_not_path_or_name(rig):
    assert rig.request("MKCOL", "/" + FOLDER).status_code == 201
    assert rig.request("PUT", f"/{FOLDER}/{MARK}", data=b"x" * 300).status_code == 201
    assert rig.request("MKCOL", f"/game/{FOLDER}").status_code == 201
    status = rig.request("GET", "/rpc/status").json()
    pending = status["uploads"]["pending"][0]
    assert "path" not in pending
    assert pending["id"] == "0x%016x" % path_id(f"{FOLDER}/{MARK}")
    unit = status["units"][0]
    assert "name" not in unit
    assert unit["id"] == "0x%016x" % path_id(f"game/{FOLDER}")


def test_status_detail_is_scrubbed(rig, monkeypatch):
    def boom(**kw):
        raise OSError(13, "denied", str(rig.cfg.upload_dir / FOLDER / MARK))

    monkeypatch.setattr(rig.resolver.api, "register", boom)
    assert rig.request("MKCOL", "/" + FOLDER).status_code == 201
    assert rig.request("PUT", f"/{FOLDER}/{MARK}", data=b"x" * 300).status_code == 201
    try:
        _upload_now(rig, FOLDER, MARK)
    except Exception:
        pass
    detail = rig.request("GET", "/rpc/status").json()["uploads"]["pending"][0]["detail"]
    assert "PRIVATE" not in detail and tag(f"{FOLDER}/{MARK}") in detail


def test_meta_files_are_not_masked(capture, cache):
    logger, stream = capture
    logger.warning("could not persist %s", cache / "meta" / "upload-queue.json")
    assert "upload-queue.json" in stream.getvalue()


# --------------------------------------------------------------------------- #
# Review fixes: one id per file everywhere, scrub keeps looking, no names in
# exception text
# --------------------------------------------------------------------------- #


def test_scrub_keeps_looking_after_an_unrelated_path_on_the_same_line(capture, cache):
    logger, stream = capture
    logger.error("D:\\python\\x.py:12: Warning while reading H:\\Secret Folder\\%s", MARK)
    out = stream.getvalue()
    assert "PRIVATE" not in out and "Secret" not in out
    assert "D:\\python\\x.py" in out


def test_paths_query_parameter_is_scrubbed_and_decoded_once(capture):
    logger, stream = capture
    logger.error("GET http://127.0.0.1:8081/rpc/game/state?paths=game%%2F%s&x=1", "Secret%2541")
    # a literal %41 in a name must hash as '%41', not as 'A'
    logger.error("GET http://127.0.0.1:8081/rpc/thumb?path=Secret%2541%2Fx.jpg")
    out = stream.getvalue()
    assert "Secret" not in out
    assert "path=" + tag("Secret%41/x.jpg") in out


def test_coverage_error_names_the_file_by_id_only():
    import types
    from pathlib import Path

    import upload_engine
    from transfer_models import TransferRequest

    engine = object.__new__(upload_engine.UploadEngine)
    engine.api = types.SimpleNamespace(check_hash=lambda fingerprint: {"files": []})
    request = TransferRequest(
        source=Path("x"), upload_name=MARK, mime_type="", parent_id=None,
        logical_size=1, log_tag=tag("somewhere/" + MARK),
    )
    with pytest.raises(upload_engine.CoverageError) as err:
        engine._assert_registered(request, "fp")
    assert "PRIVATE" not in str(err.value) and tag("somewhere/" + MARK) in str(err.value)


def test_missing_telegram_media_error_has_no_message_id():
    import asyncio
    import types

    import tgio

    async def get_messages(*a, **k):
        return [None]

    fake = types.SimpleNamespace(_client=types.SimpleNamespace(get_messages=get_messages))
    with pytest.raises(FileNotFoundError) as err:
        asyncio.run(tgio.TelegramWorker._fetch_document(fake, 80824))
    assert "80824" not in str(err.value) and "id=?" in str(err.value)


def test_7zip_stderr_member_names_do_not_reach_the_error(monkeypatch, tmp_path):
    import subprocess
    import sys

    import gamestage

    def fake_run(*a, **k):
        return types.SimpleNamespace(returncode=2, stdout="", stderr="ERROR: CRC Failed : Game\\save\\PRIVATE.dat")

    import types

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(gamestage.ArchiveExtractError) as err:
        gamestage.extract_archive(tmp_path / "a.rar", tmp_path / "out", sys.executable)
    assert "PRIVATE" not in str(err.value) and "2" in str(err.value)
